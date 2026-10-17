from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    arc_fortnox_client_id = fields.Char(
        related="company_id.arc_fortnox_client_id",
        readonly=False,
        string="Fortnox Client Id",
    )
    arc_fortnox_client_secret = fields.Char(
        related="company_id.arc_fortnox_client_secret",
        readonly=False,
        string="Fortnox Client Secret",
    )
    arc_fortnox_trigger_mode = fields.Selection(
        related="company_id.arc_fortnox_trigger_mode",
        readonly=False,
        string="Fortnox Trigger Mode",
    )
    arc_fortnox_trigger_domain = fields.Char(
        related="company_id.arc_fortnox_trigger_domain",
        readonly=False,
        string="Fortnox Trigger Domain",
    )
    arc_fortnox_trigger_since = fields.Datetime(
        related="company_id.arc_fortnox_trigger_since",
        readonly=False,
        string="Fortnox Trigger Since",
    )
    arc_fortnox_throttle_calls = fields.Integer(
        related="company_id.arc_fortnox_throttle_calls",
        readonly=False,
        string="Fortnox Calls Per Period",
    )
    arc_fortnox_throttle_seconds = fields.Integer(
        related="company_id.arc_fortnox_throttle_seconds",
        readonly=False,
        string="Fortnox Throttle Period (s)",
    )
    arc_fortnox_timeout_connect = fields.Integer(
        related="company_id.arc_fortnox_timeout_connect",
        readonly=False,
        string="Fortnox Connect Timeout (s)",
    )
    arc_fortnox_timeout_read = fields.Integer(
        related="company_id.arc_fortnox_timeout_read",
        readonly=False,
        string="Fortnox Response Timeout (s)",
    )
    arc_fortnox_backoff_minutes = fields.Char(
        related="company_id.arc_fortnox_backoff_minutes",
        readonly=False,
        string="Fortnox Backoff Schedule (min)",
    )
    arc_fortnox_max_attempts = fields.Integer(
        related="company_id.arc_fortnox_max_attempts",
        readonly=False,
        string="Fortnox Max Attempts",
    )
    arc_fortnox_max_description_length = fields.Integer(
        related="company_id.arc_fortnox_max_description_length",
        readonly=False,
        string="Fortnox Max Description Length",
    )
    arc_fortnox_send_article_numbers = fields.Boolean(
        related="company_id.arc_fortnox_send_article_numbers",
        readonly=False,
        string="Fortnox: Send Article Numbers",
    )
    arc_fortnox_freight_product_ids = fields.Many2many(
        related="company_id.arc_fortnox_freight_product_ids",
        readonly=False,
        string="Fortnox: Freight Products",
    )
    arc_fortnox_invoice_template_company = fields.Char(
        related="company_id.arc_fortnox_invoice_template_company",
        readonly=False,
        string="Fortnox: Invoice Template for Companies",
    )
    arc_fortnox_invoice_template_private = fields.Char(
        related="company_id.arc_fortnox_invoice_template_private",
        readonly=False,
        string="Fortnox: Invoice Template for Private Customers",
    )
    arc_fortnox_terms_source = fields.Selection(
        related="company_id.arc_fortnox_terms_source",
        readonly=False,
        string="Fortnox: Payment Terms From",
    )
    arc_fortnox_redirect_uri = fields.Char(
        compute="_compute_arc_fortnox_redirect_uri",
        string="Fortnox Redirect URI",
        help="Enter exactly this address as the redirect URI when you "
        "register the app in the Fortnox developer portal.",
    )

    def _compute_arc_fortnox_redirect_uri(self):
        for settings in self:
            settings.arc_fortnox_redirect_uri = (
                settings.company_id._arc_fortnox_redirect_uri()
            )

    def action_arc_fortnox_authorize(self):
        """Start the OAuth authorization in the user's browser."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": self.company_id.arc_fortnox_authorize_url(),
            "target": "self",
        }
