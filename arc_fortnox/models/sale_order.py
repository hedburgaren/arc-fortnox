import ast
import logging
import re
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools.translate import _

from .fortnox_client import FortnoxError

_logger = logging.getLogger(__name__)

GROUP_INVOICER = "arc_fortnox.group_invoicer"

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

        The customer step and the draft step run in savepoints of their
        own, one after the other: a customer created in Fortnox keeps its
        number on the partner even when the draft step fails afterwards, so
        a retry finds it instead of creating it again.
        """
        self.ensure_one()
        mapping = self._arc_fortnox_vat_mapping()
        with self.env.cr.savepoint():
            customer_number = self._arc_fortnox_resolve_customer(client)
        with self.env.cr.savepoint():
            invoice = {
                "CustomerNumber": customer_number,
                "Currency": self.currency_id.name,
                "TermsOfPayment": self._arc_fortnox_payment_term_code(),
                "ExternalInvoiceReference1": self.name,
                "VATIncluded": self._arc_fortnox_vat_included(),
                "InvoiceRows": self._arc_fortnox_invoice_rows(mapping),
            }
            if self.client_order_ref:
                # The customer's own order reference ("Er ordernummer").
                invoice["YourOrderNumber"] = self.client_order_ref
            response = client.post("invoices", {"Invoice": invoice})
        created = (response or {}).get("Invoice") or {}
        number = str(created.get("DocumentNumber") or "")
        if not number:
            raise FortnoxError(
                _("Fortnox did not return an invoice number for %s.", self.name),
                code="invoice_no_number",
            )
        return number

    def _arc_fortnox_find_existing_draft(self, client):
        """Return the number of the live Fortnox invoice for this order.

        Fortnox is searched by ExternalInvoiceReference1, which this module
        sets to the order name. Cancelled invoices are ignored, a hit is
        only adopted when its reference equals the order name exactly, and
        more than one live hit stops the entry instead of guessing. Returns
        an empty string when there is nothing to adopt.
        """
        self.ensure_one()
        found = client.get(
            "invoices", params={"externalinvoicereference1": self.name}
        )
        matches = []
        for listed in (found or {}).get("Invoices") or []:
            number = str(listed.get("DocumentNumber") or "")
            if not number:
                continue
            invoice = listed
            if "ExternalInvoiceReference1" not in invoice or "Cancelled" not in invoice:
                detail = client.get(f"invoices/{quote(number, safe='')}")
                invoice = (detail or {}).get("Invoice") or {}
            if invoice.get("Cancelled"):
                continue
            if (invoice.get("ExternalInvoiceReference1") or "") != self.name:
                continue
            matches.append(number)
        if len(matches) > 1:
            raise FortnoxError(
                _(
                    "Fortnox holds more than one live invoice with reference "
                    "%s (%s). Cancel the extra ones in Fortnox, then retry.",
                    self.name,
                    ", ".join(matches),
                ),
                code="invoice_reference_ambiguous",
            )
        return matches[0] if matches else ""

    def _action_cancel(self):
        """Cancelling an order withdraws it from the Fortnox queue.

        Pending and failed entries are cancelled. An entry in sending is
        left to the queue, which checks the order state before it sends.
        """
        result = super()._action_cancel()
        entries = self.env["arc.fortnox.queue"].sudo().search(
            [
                ("sale_order_id", "in", self.ids),
                ("state", "in", ["pending", "error"]),
            ]
        )
        entries.write({"state": "cancelled"})
        return result

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

    def _arc_fortnox_vat_included(self):
        """Whether the row prices include VAT, for the invoice header.

        Odoo prices follow the tax: a price included tax means price_unit
        carries the VAT. Fortnox has one switch per invoice, so an order
        that mixes included and excluded taxes stops before any request.
        """
        self.ensure_one()
        taxes = self.order_line.filtered(lambda line: not line.display_type).tax_id
        included = set(taxes.mapped("price_include"))
        if len(included) > 1:
            raise FortnoxError(
                _(
                    "Order %s mixes taxes that include VAT in the price with "
                    "taxes that do not. Fortnox takes one setting per invoice.",
                    self.name,
                ),
                code="vat_included_mixed",
            )
        return bool(included and included.pop())

    def _arc_fortnox_line_vat(self, line, mapping):
        """The VAT rate for a row, always explicit.

        A row without a VAT rate takes whatever Fortnox defaults to for the
        account, which is how a VAT free line ends up with 25 percent. So
        every row carries its rate: the line's percent tax, or 0 for an
        untaxed line on an order whose VAT type is not Swedish VAT. An
        untaxed line under Swedish VAT, or a tax that is not a plain
        percentage, stops the push.
        """
        tax = line.tax_id
        if not tax:
            if mapping.vat_type == "SEVAT":
                raise FortnoxError(
                    _(
                        "Order line %s has no tax, but the order is under "
                        "Swedish VAT. Set the tax on the line, 0 percent if "
                        "the line is exempt.",
                        line.name or line.id,
                    ),
                    code="line_tax_missing",
                )
            return 0
        if tax.amount_type != "percent":
            raise FortnoxError(
                _(
                    "Order line %s has a tax that is not a plain percentage. "
                    "Fortnox rows carry a VAT rate in percent.",
                    line.name or line.id,
                ),
                code="line_tax_not_percent",
            )
        return tax.amount

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
        row["VAT"] = self._arc_fortnox_line_vat(line, mapping)
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
        """Reduce an organisation number to the form used for comparison.

        Swedish forms all become the bare ten digits: 556123-4567, a
        Swedish VAT number SE556123456701 (country code and the trailing 01
        dropped), and a personal identity number written with the century,
        19800101-1234. Other values keep their characters without
        separators, uppercased, so a foreign registry number still
        compares with itself.
        """
        if not value:
            return ""
        cleaned = re.sub(r"[^A-Za-z0-9]", "", value).upper()
        if cleaned.startswith("SE") and cleaned[2:].isdigit():
            digits = cleaned[2:]
            if len(digits) == 12 and digits.endswith("01"):
                return digits[:10]
            return digits
        if cleaned.isdigit() and len(cleaned) == 12 and cleaned[:2] in ("19", "20"):
            return cleaned[2:]
        return cleaned

    def _arc_fortnox_partner_org_number(self, partner):
        """The organisation number of the partner, normalised.

        The company registry field wins; without it a Swedish VAT number is
        reduced to the organisation number it contains. A foreign VAT
        number is not an organisation number and gives nothing.
        """
        if partner.company_registry:
            return self._arc_fortnox_normalise_org_number(partner.company_registry)
        vat = re.sub(r"[^A-Za-z0-9]", "", partner.vat or "").upper()
        if vat.startswith("SE"):
            return self._arc_fortnox_normalise_org_number(vat)
        return ""

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

    def _arc_fortnox_check_customer_identity(self, customer, partner):
        """Stop when the Fortnox customer is a different legal person.

        Compared on the organisation number when both sides carry one. A
        customer number that points at somebody else must never receive
        the draft, and the matching VAT type, currency and terms of most
        domestic customers would not catch it.
        """
        ours = self._arc_fortnox_partner_org_number(partner)
        theirs = self._arc_fortnox_normalise_org_number(
            customer.get("OrganisationNumber")
        )
        if ours and theirs and ours != theirs:
            raise FortnoxError(
                _(
                    "Fortnox customer %s carries another organisation number "
                    "than the partner. Correct the Fortnox customer number "
                    "on the partner, then retry.",
                    customer.get("CustomerNumber"),
                ),
                code="customer_identity_mismatch",
            )

    def _arc_fortnox_customer_payload(self):
        """Default Fortnox customer payload. Override to adapt.

        The mapping resolvers are used deliberately: an unmapped VAT type or
        payment term stops the push before the customer is created. The
        address and the invoice email come from the order's invoice
        address, falling back to the company for what it lacks.
        """
        self.ensure_one()
        partner = self.partner_invoice_id.commercial_partner_id
        invoice_address = self.partner_invoice_id
        mapping = self._arc_fortnox_vat_mapping()
        payload = {
            "Name": partner.name,
            "Type": "COMPANY" if partner.is_company else "PRIVATE",
            "VATType": mapping.vat_type,
            "Currency": self.currency_id.name,
            "TermsOfPayment": self._arc_fortnox_payment_term_code(),
        }
        org_number = partner.company_registry or self._arc_fortnox_partner_org_number(
            partner
        )
        if org_number:
            payload["OrganisationNumber"] = org_number
        if partner.vat:
            payload["VATNumber"] = partner.vat
        address = invoice_address if invoice_address.street else partner
        if address.street:
            payload["Address1"] = address.street
        if address.street2:
            payload["Address2"] = address.street2
        if address.zip:
            payload["ZipCode"] = address.zip
        if address.city:
            payload["City"] = address.city
        if address.country_id:
            payload["CountryCode"] = address.country_id.code
        email = invoice_address.email or partner.email
        if email:
            payload["EmailInvoice"] = email
        return payload

    def _arc_fortnox_resolve_customer(self, client):
        """Return the Fortnox customer number for this order's customer.

        1. The partner carries a Fortnox customer number: the customer must
           exist there and be the same legal person; a stale or foreign
           number stops the push.
        2. No number: search Fortnox by organisation number. Exactly one
           customer with that number is adopted onto the partner without
           touching Fortnox; more than one stops the push.
        3. Still nothing: the customer is created once from the payload
           hook and the returned number is stored on the partner.

        Existing customers are never updated in v1.
        """
        self.ensure_one()
        partner = self.partner_invoice_id.commercial_partner_id.sudo()
        number = partner.arc_fortnox_customer_number
        if number:
            try:
                found = client.get(f"customers/{quote(number, safe='')}")
            except FortnoxError as error:
                if error.http_status == 404:
                    raise FortnoxError(
                        _(
                            "The partner references Fortnox customer %s, but "
                            "no such customer exists there. The push stops "
                            "instead of creating a customer on a stale "
                            "reference.",
                            number,
                        ),
                        code="customer_ref_stale",
                        http_status=404,
                    ) from error
                raise
            customer = found.get("Customer") or {}
            self._arc_fortnox_check_customer_identity(customer, partner)
            self._arc_fortnox_check_customer_mismatch(customer)
            return number

        org_number = self._arc_fortnox_partner_org_number(partner)
        if org_number:
            customers = self._arc_fortnox_search_customers(client, org_number)
            if len(customers) > 1:
                raise FortnoxError(
                    _(
                        "Fortnox has %s customers with the organisation number "
                        "of this partner. Set the right Fortnox customer number "
                        "on the partner, then retry.",
                        len(customers),
                    ),
                    code="customer_orgnr_ambiguous",
                )
            if customers:
                customer = customers[0]
                number = str(customer.get("CustomerNumber") or "")
                if number:
                    partner.write({"arc_fortnox_customer_number": number})
                    self._arc_fortnox_check_customer_mismatch(customer)
                    return number

        created = client.post(
            "customers", {"Customer": self._arc_fortnox_customer_payload()}
        )
        customer = (created or {}).get("Customer") or {}
        number = str(customer.get("CustomerNumber") or "")
        if not number:
            raise FortnoxError(
                _("Fortnox did not return a customer number for the order's customer."),
                code="customer_creation_no_number",
            )
        partner.write({"arc_fortnox_customer_number": number})
        return number

    def _arc_fortnox_search_customers(self, client, org_number):
        """Fortnox customers whose organisation number equals org_number.

        Fortnox stores the number as it was typed, with or without the
        dash, so a Swedish ten digit number is searched in both forms. The
        hits are compared after normalisation, never trusted as returned.
        """
        variants = [org_number]
        if len(org_number) == 10 and org_number.isdigit():
            variants.append(f"{org_number[:6]}-{org_number[6:]}")
        matches = {}
        for variant in variants:
            found = client.get("customers", params={"organisationnumber": variant})
            for customer in (found or {}).get("Customers") or []:
                theirs = self._arc_fortnox_normalise_org_number(
                    customer.get("OrganisationNumber")
                )
                number = str(customer.get("CustomerNumber") or "")
                if number and theirs == org_number:
                    matches[number] = customer
            if matches:
                break
        return list(matches.values())

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
            since = company.arc_fortnox_trigger_since
            if not since:
                _logger.warning(
                    "The Fortnox trigger for company %s has no start date and "
                    "was skipped; switch the automatic mode off and on, or "
                    "set the date, in Settings.",
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
                            ("date_order", ">=", since),
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
