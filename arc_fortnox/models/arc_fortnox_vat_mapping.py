from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.translate import _

VAT_TYPES = [
    ("SEVAT", "SEVAT"),
    ("EUREVERSEDVAT", "EUREVERSEDVAT"),
    ("EUVAT", "EUVAT"),
    ("EXPORT", "EXPORT"),
]


class ArcFortnoxVatMapping(models.Model):
    """Fiscal position to Fortnox VAT type, with a default revenue account.

    One active row per fiscal position, plus at most one row without a
    fiscal position for orders that carry none. The VAT type and the default
    revenue account are decided here instead of being guessed, so an
    unmapped fiscal position stops the push before any request is made.
    """

    _name = "arc.fortnox.vat.mapping"
    _description = "Fortnox VAT Mapping"
    _order = "sequence, id"
    _rec_name = "fiscal_position_id"

    sequence = fields.Integer(string="Sequence", default=10)
    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    fiscal_position_id = fields.Many2one(
        comodel_name="account.fiscal.position",
        string="Fiscal Position",
        index=True,
        ondelete="cascade",
        help="Leave empty for the row that applies to orders without a "
        "fiscal position, usually domestic customers.",
    )
    vat_type = fields.Selection(
        selection=VAT_TYPES,
        string="Fortnox VAT Type",
        required=True,
        help="SEVAT for Swedish VAT, EUREVERSEDVAT for EU reverse VAT, "
        "EUVAT for EU VAT, EXPORT for outside the EU.",
    )
    revenue_account = fields.Char(
        string="Default Revenue Account",
        help="Fortnox account number used when neither the product nor a "
        "hook provides an account for the row.",
    )
    active = fields.Boolean(string="Active", default=True)

    _sql_constraints = [
        (
            "fiscal_position_company_uniq",
            "unique(fiscal_position_id, company_id)",
            "Only one Fortnox VAT mapping per fiscal position and company.",
        ),
    ]

    def init(self):
        # The unique constraint above treats empty fiscal positions as
        # distinct; one row without a fiscal position per company.
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS arc_fortnox_vat_mapping_no_fp_uniq
            ON arc_fortnox_vat_mapping (company_id)
            WHERE fiscal_position_id IS NULL
            """
        )

    @api.depends("fiscal_position_id")
    def _compute_display_name(self):
        for mapping in self:
            mapping.display_name = (
                mapping.fiscal_position_id.display_name
                or _("Without fiscal position")
            )

    @api.constrains("revenue_account")
    def _check_revenue_account(self):
        for mapping in self:
            raw = (mapping.revenue_account or "").strip()
            if raw and not raw.isdigit():
                raise ValidationError(
                    _(
                        "The Fortnox revenue account must be a bare account "
                        "number, for example 3011."
                    )
                )
