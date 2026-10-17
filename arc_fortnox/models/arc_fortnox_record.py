import difflib
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .sale_order import GROUP_INVOICER

RECORD_TYPES = [("customer", "Customer"), ("article", "Article")]
TRUTH_SOURCES = [("fortnox", "Fortnox"), ("odoo", "Odoo")]
NAME_SUGGESTION_CUTOFF = 0.85
FETCH_PAGE_LIMIT = 500


LEGAL_FORMS = {"ab", "aktiebolag", "aktiebolaget", "ef", "hb", "kb", "ek", "för"}


def normalise_name(value):
    """Name for comparison: case, punctuation and legal form ignored.

    "Aktiebolaget Mähler & Söner" and "AB Mähler & Söner" compare equal, as
    do "Enskild Firma Kim Andersson" and "Kim Andersson".
    """
    value = (value or "").casefold().replace("enskild firma", " ")
    words = re.sub(r"[^\w]+", " ", value).split()
    return " ".join(word for word in words if word not in LEGAL_FORMS)


class ArcFortnoxRecord(models.Model):
    """A Fortnox customer or article, read from Fortnox, mapped to Odoo.

    The Fortnox side is read only: it comes from the API or from a Fortnox
    export and is never written back. The mapping is stored in Odoo only,
    as the Fortnox customer number on the partner and the article number
    on the product. Each row suggests an Odoo record (organisation number or
    a close name for customers, the internal reference for articles); the
    invoicer confirms or picks another.
    """

    _name = "arc.fortnox.record"
    _description = "Fortnox Record Mapping"
    _order = "record_type, state_sequence, name, id"
    _rec_name = "name"

    company_id = fields.Many2one(
        "res.company", string="Company", required=True, index=True,
        default=lambda self: self.env.company,
    )
    record_type = fields.Selection(RECORD_TYPES, string="Type", required=True, index=True)
    fortnox_number = fields.Char(string="Fortnox Number", required=True, readonly=True)
    name = fields.Char(string="Fortnox Name", readonly=True)
    organisation_number = fields.Char(string="Fortnox Organisation Number", readonly=True)
    email = fields.Char(string="Fortnox Email", readonly=True)
    city = fields.Char(string="Fortnox City", readonly=True)
    customer_type = fields.Char(string="Fortnox Customer Type", readonly=True)
    fortnox_active = fields.Boolean(string="Active in Fortnox", readonly=True, default=True)

    partner_id = fields.Many2one(
        "res.partner", string="Odoo Customer", index=True,
        domain="[('is_company', '=', True), ('parent_id', '=', False)]",
    )
    product_id = fields.Many2one("product.product", string="Odoo Product", index=True)
    suggested_partner_id = fields.Many2one("res.partner", string="Suggested Customer", readonly=True)
    suggested_product_id = fields.Many2one(
        "product.product", string="Suggested Product", readonly=True
    )
    suggestion_reason = fields.Char(string="Why Suggested", readonly=True)
    state = fields.Selection(
        [("open", "Open"), ("suggested", "Suggested"), ("linked", "Linked")],
        string="State", compute="_compute_state", store=True,
    )
    state_sequence = fields.Integer(compute="_compute_state", store=True)
    difference = fields.Char(string="Differs", compute="_compute_difference")
    truth_source = fields.Selection(TRUTH_SOURCES, compute="_compute_difference")

    _sql_constraints = [
        (
            "fortnox_number_uniq",
            "unique(company_id, record_type, fortnox_number)",
            "A Fortnox number appears once per type and company.",
        )
    ]

    @api.depends(
        "partner_id", "product_id", "suggested_partner_id", "suggested_product_id"
    )
    def _compute_state(self):
        for record in self:
            if record.partner_id or record.product_id:
                record.state, record.state_sequence = "linked", 3
            elif record.suggested_partner_id or record.suggested_product_id:
                record.state, record.state_sequence = "suggested", 1
            else:
                record.state, record.state_sequence = "open", 2

    @api.depends(
        "name", "organisation_number", "partner_id.name", "partner_id.company_registry",
        "product_id.name", "product_id.product_template_attribute_value_ids",
        "company_id.arc_fortnox_truth_customer",
        "company_id.arc_fortnox_truth_article",
    )
    def _compute_difference(self):
        for record in self:
            company = record.company_id.sudo()
            notes = []
            if record.record_type == "customer":
                record.truth_source = company.arc_fortnox_truth_customer
                partner = record.partner_id
                if partner:
                    if normalise_name(partner.name) != normalise_name(record.name):
                        notes.append(_("name"))
                    theirs = re.sub(r"[^0-9]", "", record.organisation_number or "")
                    ours = re.sub(r"[^0-9]", "", partner.company_registry or "")
                    if theirs and ours and theirs[-10:] != ours[-10:]:
                        notes.append(_("organisation number"))
            else:
                record.truth_source = company.arc_fortnox_truth_article
                product = record.product_id
                label = product.with_context(display_default_code=False).display_name
                if product and normalise_name(label) != normalise_name(record.name):
                    notes.append(_("name"))
            record.difference = ", ".join(notes)

    # ------------------------------------------------------------------
    # Linking. Only the Odoo side is written.
    # ------------------------------------------------------------------

    def _check_invoicer(self):
        if not self.env.user.has_group(GROUP_INVOICER):
            raise AccessError(_("Only Fortnox invoicers may map Fortnox records."))

    def write(self, vals):
        linking = {"partner_id", "product_id"} & set(vals)
        if linking:
            self._check_invoicer()
            before = {record.id: (record.partner_id, record.product_id) for record in self}
        result = super().write(vals)
        if linking:
            for record in self:
                record._arc_fortnox_sync_link(*before[record.id])
        return result

    def _arc_fortnox_sync_link(self, old_partner, old_product):
        """Write the Fortnox number on the newly linked Odoo record."""
        self.ensure_one()
        number = self.fortnox_number
        if self.record_type == "customer":
            if old_partner and old_partner != self.partner_id:
                if old_partner.sudo().arc_fortnox_customer_number == number:
                    old_partner.sudo().arc_fortnox_customer_number = False
            partner = self.partner_id.sudo()
            if partner and partner.arc_fortnox_customer_number != number:
                if partner.arc_fortnox_customer_number:
                    raise UserError(
                        _(
                            "%(partner)s is already linked to Fortnox customer "
                            "%(other)s. Unlink that row first.",
                            partner=partner.display_name,
                            other=partner.arc_fortnox_customer_number,
                        )
                    )
                other = partner.search(
                    [("arc_fortnox_customer_number", "=", number), ("id", "!=", partner.id)],
                    limit=1,
                )
                if other:
                    raise UserError(
                        _(
                            "Fortnox customer %(number)s is already linked to %(other)s.",
                            number=number,
                            other=other.display_name,
                        )
                    )
                partner.arc_fortnox_customer_number = number
        else:
            if old_product and old_product != self.product_id:
                if old_product.sudo().arc_fortnox_article == number:
                    old_product.sudo().arc_fortnox_article = False
            product = self.product_id.sudo()
            if product and product.arc_fortnox_article != number:
                other = product.search(
                    [("arc_fortnox_article", "=", number), ("id", "!=", product.id)], limit=1
                )
                if other:
                    raise UserError(
                        _(
                            "Fortnox article %(number)s is already linked to %(other)s.",
                            number=number,
                            other=other.display_name,
                        )
                    )
                product.arc_fortnox_article = number

    def action_use_suggestion(self):
        for record in self:
            if record.record_type == "customer" and record.suggested_partner_id:
                record.partner_id = record.suggested_partner_id
            elif record.record_type == "article" and record.suggested_product_id:
                record.product_id = record.suggested_product_id
        return True

    def action_unlink_odoo(self):
        self.write({"partner_id": False, "product_id": False})
        return True

    def action_copy_from_fortnox(self):
        """Fortnox is right: correct the linked Odoo customer's name and number.

        Only for customers, only when the company treats Fortnox as the
        source of truth for customers. An organisation number is copied only
        when it is a Swedish one; Odoo is written, Fortnox never.
        """
        self._check_invoicer()
        for record in self.filtered(lambda r: r.record_type == "customer" and r.partner_id):
            if record.company_id.sudo().arc_fortnox_truth_customer != "fortnox":
                raise UserError(_("Odoo is the source of truth for customers in this company."))
            values = {"name": record.name}
            if re.fullmatch(r"\d{6}-?\d{4}", record.organisation_number or ""):
                digits = re.sub(r"[^0-9]", "", record.organisation_number)
                values["company_registry"] = "%s-%s" % (digits[:6], digits[6:])
            record.partner_id.sudo().write(values)
        return True

    # ------------------------------------------------------------------
    # Loading the Fortnox side, and suggestions.
    # ------------------------------------------------------------------

    @api.model
    def _arc_fortnox_upsert(self, company, record_type, rows):
        """Create or refresh rows from parsed Fortnox data, then suggest.

        rows: dicts with fortnox_number, name and the optional customer
        fields. Existing rows keep their link; only the Fortnox side is
        refreshed. Returns the rows touched.
        """
        existing = {
            record.fortnox_number: record
            for record in self.search(
                [("company_id", "=", company.id), ("record_type", "=", record_type)]
            )
        }
        touched = self.browse()
        fortnox_fields = (
            "name", "organisation_number", "email", "city", "customer_type", "fortnox_active",
        )
        for row in rows:
            number = (row.get("fortnox_number") or "").strip()
            if not number:
                continue
            values = {key: row[key] for key in fortnox_fields if key in row}
            record = existing.get(number)
            if record:
                super(ArcFortnoxRecord, record).write(values)
            else:
                record = self.create(
                    dict(values, company_id=company.id, record_type=record_type, fortnox_number=number)
                )
                existing[number] = record
            touched |= record
        touched._arc_fortnox_link_existing()
        touched._arc_fortnox_suggest()
        return touched

    def _arc_fortnox_link_existing(self):
        """Rows whose number Odoo already carries are linked to that record."""
        customers = self.filtered(lambda r: r.record_type == "customer" and not r.partner_id)
        if customers:
            partners = self.env["res.partner"].with_context(active_test=False).search(
                [("arc_fortnox_customer_number", "in", customers.mapped("fortnox_number"))]
            )
            by_number = {partner.arc_fortnox_customer_number: partner for partner in partners}
            for record in customers:
                if record.fortnox_number in by_number:
                    super(ArcFortnoxRecord, record).write(
                        {"partner_id": by_number[record.fortnox_number].id}
                    )
        articles = self.filtered(lambda r: r.record_type == "article" and not r.product_id)
        if articles:
            products = self.env["product.product"].with_context(active_test=False).search(
                [("arc_fortnox_article", "in", articles.mapped("fortnox_number"))]
            )
            by_number = {product.arc_fortnox_article: product for product in products}
            for record in articles:
                if record.fortnox_number in by_number:
                    super(ArcFortnoxRecord, record).write(
                        {"product_id": by_number[record.fortnox_number].id}
                    )

    def _arc_fortnox_suggest(self):
        """Suggest an Odoo record for each unlinked row.

        Customers: the organisation number first (company registry, or a
        Swedish VAT number), then a close name among companies without a
        Fortnox number. Articles: the product whose internal reference is the
        article number. A suggestion is never applied without the invoicer.
        """
        Partner = self.env["res.partner"]
        customers = self.filtered(lambda r: r.record_type == "customer" and not r.partner_id)
        if customers:
            normalise = self.env["sale.order"]._arc_fortnox_normalise_org_number
            companies = Partner.search(
                [("is_company", "=", True), ("parent_id", "=", False)]
            )
            by_orgnr, by_name = {}, {}
            for partner in companies:
                for value in (partner.company_registry, partner.vat):
                    key = normalise(value)
                    if key:
                        by_orgnr.setdefault(key, partner)
                if not partner.arc_fortnox_customer_number:
                    by_name.setdefault(normalise_name(partner.name), partner)
            names = list(by_name)
            for record in customers:
                partner, reason = Partner, ""
                key = normalise(record.organisation_number)
                if key and key in by_orgnr:
                    partner, reason = by_orgnr[key], _("organisation number")
                else:
                    close = difflib.get_close_matches(
                        normalise_name(record.name), names, n=1, cutoff=NAME_SUGGESTION_CUTOFF
                    )
                    if close:
                        partner, reason = by_name[close[0]], _("similar name")
                super(ArcFortnoxRecord, record).write(
                    {"suggested_partner_id": partner.id, "suggestion_reason": reason}
                )
        articles = self.filtered(lambda r: r.record_type == "article" and not r.product_id)
        if articles:
            products = self.env["product.product"].search(
                [("default_code", "in", articles.mapped("fortnox_number"))]
            )
            by_code = {product.default_code: product for product in products}
            for record in articles:
                product = by_code.get(record.fortnox_number, self.env["product.product"])
                super(ArcFortnoxRecord, record).write(
                    {
                        "suggested_product_id": product.id,
                        "suggestion_reason": _("internal reference") if product else "",
                    }
                )

    @api.model
    def action_arc_fortnox_fetch(self, record_type):
        """Read all customers or articles from the Fortnox API, page by page."""
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators may load records from Fortnox."))
        company = self.env.company
        client = company.arc_fortnox_client()
        path, key = ("customers", "Customers") if record_type == "customer" else ("articles", "Articles")
        rows, page = [], 1
        while True:
            found = client.get(path, params={"page": page, "limit": FETCH_PAGE_LIMIT}) or {}
            for item in found.get(key) or []:
                rows.append(self._arc_fortnox_row_from_api(record_type, item))
            meta = found.get("MetaInformation") or {}
            if page >= int(meta.get("@TotalPages") or 1):
                break
            page += 1
        self._arc_fortnox_upsert(company, record_type, rows)
        return True

    def action_arc_fortnox_fetch_customers(self):
        return self.action_arc_fortnox_fetch("customer")

    def action_arc_fortnox_fetch_articles(self):
        return self.action_arc_fortnox_fetch("article")

    @api.model
    def _arc_fortnox_row_from_api(self, record_type, item):
        if record_type == "customer":
            return {
                "fortnox_number": str(item.get("CustomerNumber") or ""),
                "name": item.get("Name") or "",
                "organisation_number": item.get("OrganisationNumber") or "",
                "email": item.get("Email") or "",
                "city": item.get("City") or "",
                "fortnox_active": item.get("Active", True) is not False,
            }
        return {
            "fortnox_number": str(item.get("ArticleNumber") or ""),
            "name": item.get("Description") or "",
            "fortnox_active": item.get("Active", True) is not False,
        }
