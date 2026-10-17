import ast
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools.translate import _

from .fortnox_client import FortnoxError

_logger = logging.getLogger(__name__)

GROUP_INVOICER = "arc_fortnox.group_invoicer"

COUNTRY_PREFIXES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE",
}


class SaleOrder(models.Model):
    _inherit = "sale.order"

    arc_fortnox_invoice_number = fields.Char(
        string="Fortnox Invoice Number",
        copy=False,
        readonly=True,
        help="Set when the Fortnox invoice draft exists.",
    )
    arc_fortnox_active_queue_count = fields.Integer(
        string="Active Fortnox Queue Entries",
        compute="_compute_arc_fortnox_active_queue_count",
    )
    arc_fortnox_queue_ids = fields.One2many(
        comodel_name="arc.fortnox.queue",
        inverse_name="sale_order_id",
        string="Fortnox Queue Entries",
    )

    @api.depends("arc_fortnox_invoice_number", "arc_fortnox_queue_ids.state")
    def _compute_arc_fortnox_active_queue_count(self):
        counts = {}
        if self.ids:
            data = self.env["arc.fortnox.queue"].sudo()._read_group(
                [
                    ("sale_order_id", "in", self.ids),
                    ("state", "in", ["pending", "sending", "error"]),
                ],
                ["sale_order_id"],
                ["__count"],
            )
            counts = {row[0].id: row[1] for row in data}
        for order in self:
            order.arc_fortnox_active_queue_count = counts.get(order.id, 0)

    # Extension points. The open core implements neutral defaults; private
    # companion modules override them to wire their own behavior.

    def _arc_fortnox_enqueue_allowed(self):
        """Extra gate before an order may be enqueued. Override to refuse."""
        self.ensure_one()
        return True

    def _arc_fortnox_invoice_created(self):
        """Called when the Fortnox draft exists and the number is written."""
        self.ensure_one()

    def _arc_fortnox_invoice_disconnected(self):
        """Called after the order was disconnected from a Fortnox draft."""
        self.ensure_one()

    def _arc_fortnox_push_draft(self, client):
        """Create the invoice draft in Fortnox and return its number.

        Draft only: the payload never books, sends, prints or emails. The
        human does that from Fortnox.
        """
        self.ensure_one()
        mapping = self._arc_fortnox_vat_mapping()
        customer_number = self._arc_fortnox_resolve_customer(client)
        payload = {
            "Invoice": {
                "CustomerNumber": customer_number,
                "Currency": self.currency_id.name,
                "TermsOfPayment": self._arc_fortnox_payment_term_code(),
                "YourOrderNumber": self.name,
                "ExternalInvoiceReference1": self.name,
                "InvoiceRows": self._arc_fortnox_invoice_rows(mapping),
            }
        }
        response = client.post("invoices", payload)
        invoice = (response or {}).get("Invoice") or {}
        number = str(invoice.get("DocumentNumber") or "")
        if not number:
            raise FortnoxError(
                _("Fortnox did not return an invoice number for %s.", self.name),
                code="invoice_no_number",
            )
        return number

    def _arc_fortnox_invoice_rows(self, mapping):
        """Build the Fortnox invoice rows from the order lines.

        Section and note rows become text rows with no price and no
        quantity. Descriptions travel verbatim; above the configured
        maximum the text continues on continuation rows. Freight,
        packaging and cutting are ordinary order lines and become ordinary
        rows: the header fields Freight and AdministrationFee are not used.
        """
        self.ensure_one()
        max_length = self.company_id.sudo().arc_fortnox_max_description_length
        rows = []
        for line in self.order_line:
            if line.display_type in ("line_section", "line_note"):
                rows.append(
                    {
                        "DeliveredQuantity": 0,
                        "Price": 0,
                        "Description": line.name or "",
                    }
                )
                continue
            if len(line.tax_id) > 1:
                raise FortnoxError(
                    _(
                        "Order line %s has more than one tax. Fortnox rows "
                        "carry one VAT rate each.",
                        line.name or line.id,
                    ),
                    code="line_multiple_taxes",
                )
            row = self._arc_fortnox_line_values(line, mapping)
            description = row.get("Description") or ""
            remainder = ""
            if max_length and len(description) > max_length:
                row["Description"], remainder = (
                    description[:max_length],
                    description[max_length:],
                )
            rows.append(row)
            while remainder:
                chunk, remainder = (
                    remainder[:max_length],
                    remainder[max_length:],
                )
                rows.append(
                    {
                        "DeliveredQuantity": 0,
                        "Price": 0,
                        "Description": chunk,
                    }
                )
        return rows

    def _arc_fortnox_line_values(self, line, mapping):
        """Default invoice row payload for one order line. Override to adapt.

        Account resolution order: product field, then the VAT mapping
        default. Anything unmapped stops the push before the request.
        """
        self.ensure_one()
        row = {
            "Description": line.name or "",
            "DeliveredQuantity": line.product_uom_qty,
            "Price": line.price_unit,
            "Unit": line._arc_fortnox_unit_code(),
            "AccountNumber": line._arc_fortnox_row_account(mapping),
        }
        if line.discount:
            row["Discount"] = line.discount
        if line.tax_id:
            row["VAT"] = line.tax_id[0].amount
        if self.company_id.sudo().arc_fortnox_send_article_numbers:
            article = line.product_id.arc_fortnox_article
            if article:
                row["ArticleNumber"] = article
        return row

    # -------------------------------------------------------------
    # Mapping resolution. These helpers raise a FortnoxError before any
    # request is made when a value is unmapped: never a default.
    # -------------------------------------------------------------

    def _arc_fortnox_vat_mapping(self):
        """Return the active Fortnox VAT mapping for the order."""
        self.ensure_one()
        mapping = self.env["arc.fortnox.vat.mapping"].sudo().search(
            [
                ("company_id", "=", self.company_id.id),
                ("fiscal_position_id", "=", self.fiscal_position_id.id),
                ("active", "=", True),
            ],
            limit=1,
        )
        if not mapping:
            raise FortnoxError(
                _(
                    "No Fortnox VAT mapping for fiscal position %s on "
                    "company %s.",
                    self.fiscal_position_id.display_name or "none",
                    self.company_id.name,
                ),
                code="vat_mapping_missing",
            )
        return mapping

    def _arc_fortnox_payment_term_code(self):
        """Return the Fortnox terms of payment code for the order."""
        self.ensure_one()
        code = self.payment_term_id.arc_fortnox_code
        if not code:
            raise FortnoxError(
                _(
                    "Payment term %s has no Fortnox payment term code.",
                    self.payment_term_id.display_name or "none",
                ),
                code="payment_term_unmapped",
            )
        return code

    # -------------------------------------------------------------
    # Customer step. v1 never overwrites an existing Fortnox customer.
    # -------------------------------------------------------------

    @staticmethod
    def _arc_fortnox_normalise_org_number(value):
        """Compare organisation numbers ignoring case, dashes and spaces.

        A leading country code is dropped so that Odoo's VAT form
        (SE556123456701) matches Fortnox' bare organisation number
        (5561234567).
        """
        if not value:
            return ""
        cleaned = re.sub(r"[^A-Za-z0-9]", "", value).upper()
        if len(cleaned) > 10 and cleaned[:2] in COUNTRY_PREFIXES:
            cleaned = cleaned[2:]
        return cleaned

    def _arc_fortnox_check_customer_mismatch(self, customer):
        """Stop with a distinct error when Fortnox disagrees with the order.

        Empty values on the Fortnox side mean the customer simply carries no
        data for that field, which is not a disagreement. Existing customers
        are never written to, so the push stops instead.
        """
        self.ensure_one()
        mapping = self._arc_fortnox_vat_mapping()
        mapping_vat_type = mapping.vat_type
        fortnox_vat_type = customer.get("VATType") or ""
        if (
            fortnox_vat_type
            and fortnox_vat_type != mapping_vat_type
        ):
            raise FortnoxError(
                _(
                    "Customer %s has VAT type %s in Fortnox but the order "
                    "mapping says %s. Fix one of the two and retry.",
                    customer.get("CustomerNumber"),
                    fortnox_vat_type,
                    mapping_vat_type,
                ),
                code="customer_vat_type_mismatch",
            )
        fortnox_currency = (customer.get("Currency") or "").strip()
        order_currency = self.currency_id.name
        if fortnox_currency and fortnox_currency != order_currency:
            raise FortnoxError(
                _(
                    "Customer %s has currency %s in Fortnox but the order "
                    "is in %s. Fix one of the two and retry.",
                    customer.get("CustomerNumber"),
                    fortnox_currency,
                    order_currency,
                ),
                code="customer_currency_mismatch",
            )
        fortnox_terms = str(customer.get("TermsOfPayment") or "").strip()
        order_terms = self._arc_fortnox_payment_term_code()
        if fortnox_terms and fortnox_terms != order_terms:
            raise FortnoxError(
                _(
                    "Customer %s has payment terms %s in Fortnox but the "
                    "order maps to %s. Fix one of the two and retry.",
                    customer.get("CustomerNumber"),
                    fortnox_terms,
                    order_terms,
                ),
                code="customer_payment_term_mismatch",
            )

    def _arc_fortnox_customer_payload(self):
        """Default Fortnox customer payload. Override to adapt.

        The mapping resolvers are used deliberately: an unmapped VAT type or
        payment term stops the push before the customer is created.
        """
        self.ensure_one()
        partner = self.partner_invoice_id.commercial_partner_id
        mapping = self._arc_fortnox_vat_mapping()
        payload = {
            "Name": partner.name,
            "VATType": mapping.vat_type,
            "Currency": self.currency_id.name,
            "TermsOfPayment": self._arc_fortnox_payment_term_code(),
        }
        if partner.vat:
            payload["OrganisationNumber"] = partner.vat
            payload["VATNumber"] = partner.vat
        address = partner
        if address.street:
            payload["Address1"] = address.street
        if address.zip:
            payload["ZipCode"] = address.zip
        if address.city:
            payload["City"] = address.city
        if address.country_id:
            payload["CountryCode"] = address.country_id.code
        if partner.email:
            payload["EmailInvoice"] = partner.email
        return payload

    def _arc_fortnox_resolve_customer(self, client):
        """Return the Fortnox customer number for this order's customer.

        1. The partner reference holds a Fortnox customer number: the
           customer must exist there, a stale reference stops the push.
        2. No reference: search Fortnox by organisation number. A hit is
           adopted into the reference without touching Fortnox.
        3. Still nothing: the customer is created once from the payload
           hook and the returned number is stored in the reference.

        Existing customers are never updated in v1.
        """
        self.ensure_one()
        partner = self.partner_invoice_id.commercial_partner_id.sudo()
        if partner.ref:
            duplicates = self.env["res.partner"].sudo().search_count(
                [("ref", "=", partner.ref), ("id", "!=", partner.id)]
            )
            if duplicates:
                raise FortnoxError(
                    _(
                        "Reference %s is used by more than one partner. "
                        "Fortnox cannot be resolved for %s until it is "
                        "unique.",
                        partner.ref,
                        partner.display_name,
                    ),
                    code="customer_ref_duplicate",
                )
            try:
                found = client.get(f"customers/{partner.ref}")
            except FortnoxError as error:
                if error.http_status == 404:
                    raise FortnoxError(
                        _(
                            "Partner %s references Fortnox customer %s, but "
                            "no such customer exists there. The push stops "
                            "instead of creating a customer on a stale "
                            "reference.",
                            partner.display_name,
                            partner.ref,
                        ),
                        code="customer_ref_stale",
                        http_status=404,
                    ) from error
                raise
            customer = found.get("Customer") or {}
            self._arc_fortnox_check_customer_mismatch(customer)
            return partner.ref

        normalised = self._arc_fortnox_normalise_org_number(partner.vat)
        if normalised:
            found = client.get(
                "customers", params={"organisationnumber": normalised}
            )
            customers = (found or {}).get("Customers") or []
            if customers:
                number = str(customers[0].get("CustomerNumber") or "")
                if number:
                    partner.write({"ref": number})
                    customer = customers[0]
                    self._arc_fortnox_check_customer_mismatch(customer)
                    return number

        created = client.post(
            "customers", {"Customer": self._arc_fortnox_customer_payload()}
        )
        customer = (created or {}).get("Customer") or {}
        number = str(customer.get("CustomerNumber") or "")
        if not number:
            raise FortnoxError(
                _("Fortnox did not return a customer number for %s.",
                  partner.display_name),
                code="customer_creation_no_number",
            )
        partner.write({"ref": number})
        return number

    @api.model
    def _arc_fortnox_cron_auto_enqueue(self):
        """Enqueue confirmed orders per the company trigger settings.

        Runs from a cron every 5 minutes. The trigger domain is company
        specific and validated on save; a broken domain is skipped with a
        warning rather than blocking other companies.
        """
        for company in self.env["res.company"].sudo().search([]):
            if company.arc_fortnox_trigger_mode != "domain":
                continue
            raw = (company.arc_fortnox_trigger_domain or "").strip()
            if not raw:
                continue
            try:
                domain = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                _logger.warning(
                    "The Fortnox trigger domain for company %s is invalid "
                    "and was skipped.",
                    company.name,
                )
                continue
            orders = self.sudo().search(
                expression.AND(
                    [
                        domain,
                        [
                            ("state", "=", "sale"),
                            ("arc_fortnox_invoice_number", "=", False),
                            ("company_id", "=", company.id),
                        ],
                    ]
                )
            )
            eligible = orders.filtered(
                lambda order: order._arc_fortnox_enqueue_allowed()
            )
            self.env["arc.fortnox.queue"]._arc_fortnox_enqueue(eligible)

    def _arc_fortnox_check_enqueue_allowed(self):
        """Raise when the order cannot be enqueued, with a readable reason."""
        self.ensure_one()
        if self.state != "sale":
            raise UserError(
                _(
                    "Order %s is not confirmed, so it cannot be queued for "
                    "Fortnox invoicing.",
                    self.name,
                )
            )
        if self.arc_fortnox_invoice_number:
            raise UserError(
                _(
                    "Order %s already has Fortnox invoice number %s.",
                    self.name,
                    self.arc_fortnox_invoice_number,
                )
            )
        # Checked against the database, not against the display counter:
        # the counter is for the interface, this gate must not be stale.
        active_entry = self.env["arc.fortnox.queue"].sudo().search_count(
            [
                ("sale_order_id", "=", self.id),
                ("state", "in", ["pending", "sending", "error"]),
            ]
        )
        if active_entry:
            raise UserError(
                _(
                    "Order %s already has an active Fortnox queue entry.",
                    self.name,
                )
            )
        if not self._arc_fortnox_enqueue_allowed():
            raise UserError(
                _(
                    "Order %s may not be queued for Fortnox invoicing in its "
                    "current state.",
                    self.name,
                )
            )

    def action_arc_fortnox_enqueue(self):
        """Queue the orders for Fortnox invoicing. No API call is made."""
        if not self.env.user.has_group(GROUP_INVOICER):
            raise AccessError(
                _(
                    "Only Fortnox invoicers may queue orders for Fortnox "
                    "invoicing."
                )
            )
        for order in self:
            order._arc_fortnox_check_enqueue_allowed()
        entries = self.env["arc.fortnox.queue"]._arc_fortnox_enqueue(self)
        for order in entries.mapped("sale_order_id"):
            order.message_post(
                body=_("Queued for Fortnox invoicing."),
            )
        return True

    def action_arc_fortnox_disconnect(self):
        """Detach the order from its Fortnox draft.

        Allowed only when the invoice is missing in Fortnox or cancelled
        there. An existing, live draft must first be deleted or cancelled
        in Fortnox: Odoo never writes to existing Fortnox documents.
        """
        if not self.env.user.has_group(GROUP_INVOICER):
            raise AccessError(
                _("Only Fortnox invoicers may disconnect a Fortnox invoice.")
            )
        for order in self:
            if not order.arc_fortnox_invoice_number:
                raise UserError(
                    _(
                        "Order %s has no Fortnox invoice number to "
                        "disconnect.",
                        order.name,
                    )
                )
        for order in self:
            order._arc_fortnox_disconnect_one()
        return True

    def _arc_fortnox_disconnect_one(self):
        self.ensure_one()
        number = self.arc_fortnox_invoice_number
        client = self.company_id.arc_fortnox_client()
        try:
            found = client.get(f"invoices/{number}")
        except FortnoxError as error:
            if error.http_status != 404:
                raise
            found = None
        if found:
            invoice = found.get("Invoice") or {}
            if not invoice.get("Cancelled"):
                raise UserError(
                    _(
                        "Fortnox invoice %s still exists and is not "
                        "cancelled. Delete or cancel it in Fortnox first; "
                        "Odoo never writes to an existing Fortnox "
                        "document.",
                        number,
                    )
                )
        self.write({"arc_fortnox_invoice_number": False})
        self._arc_fortnox_invoice_disconnected()
        self.message_post(body=_("Disconnected from Fortnox invoice %s.", number))


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    def _arc_fortnox_unit_code(self):
        """Return the Fortnox unit code for the line's unit of measure."""
        self.ensure_one()
        code = self.product_uom.arc_fortnox_unit
        if not code:
            raise FortnoxError(
                _("Unit %s has no Fortnox unit code.",
                  self.product_uom.display_name or "none"),
                code="unit_unmapped",
            )
        return code

    def _arc_fortnox_row_account(self, mapping):
        """Return the Fortnox account for the row: product field, then the
        VAT mapping default. Unresolved stops the push."""
        self.ensure_one()
        account = self.product_id.arc_fortnox_account or mapping.revenue_account
        if not account:
            raise FortnoxError(
                _(
                    "No Fortnox account for %s: the product has no account "
                    "and the VAT mapping has no default.",
                    self.product_id.display_name or self.name or "this row",
                ),
                code="account_missing",
            )
        return account
