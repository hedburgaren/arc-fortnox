import ast
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import config as odoo_config
from odoo.tools.translate import _

from .arc_fortnox_credential import AUTHORIZE_SCOPE
from .fortnox_client import OAUTH_AUTHORIZE_URL, FortnoxClient

TRIGGER_MODES = [
    ("manual", "Manual only"),
    ("domain", "Automatic on order domain"),
]

CLIENT_ID_PARAM_NAMES = ("arc_fortnox.client_id", "arc_fortnox_client_id")
CLIENT_SECRET_PARAM_NAMES = ("arc_fortnox.client_secret", "arc_fortnox_client_secret")
REDIRECT_URI_PARAM = "arc_fortnox.redirect_uri"


class ResCompany(models.Model):
    _inherit = "res.company"

    # Connection credentials. The secret fields carry field level groups so
    # they are only read or written by administrators; the odoo.conf fallback
    # keeps environment storage possible for operators who prefer it.
    arc_fortnox_client_id = fields.Char(
        string="Fortnox Client Id",
        groups="base.group_system",
        copy=False,
    )
    arc_fortnox_client_secret = fields.Char(
        string="Fortnox Client Secret",
        groups="base.group_system",
        copy=False,
    )

    # Behavior settings. All values are per company and editable in Settings;
    # nothing about the bridge is hardcoded.
    arc_fortnox_trigger_mode = fields.Selection(
        selection=TRIGGER_MODES,
        string="Fortnox Trigger Mode",
        default="manual",
        required=True,
        help="Manual only: orders are enqueued from the button on the order. "
        "Automatic on order domain: the cron also enqueues confirmed orders "
        "matching the trigger domain.",
    )
    arc_fortnox_trigger_domain = fields.Char(
        string="Fortnox Trigger Domain",
        default="",
        help="Python domain on sale.order used when the trigger mode is "
        "automatic, for example [('arc_order_stage', '=', 'delivered')]. "
        "Fields from other modules may be used here.",
    )
    arc_fortnox_throttle_calls = fields.Integer(
        string="Fortnox Calls Per Period",
        default=25,
        help="Maximum Fortnox API calls per throttle period.",
    )
    arc_fortnox_throttle_seconds = fields.Integer(
        string="Fortnox Throttle Period (s)",
        default=5,
        help="Length of the throttle period in seconds.",
    )
    arc_fortnox_timeout_connect = fields.Integer(
        string="Fortnox Connect Timeout (s)",
        default=10,
        help="Seconds to wait for the connection to Fortnox.",
    )
    arc_fortnox_timeout_read = fields.Integer(
        string="Fortnox Response Timeout (s)",
        default=30,
        help="Seconds to wait for a response from Fortnox.",
    )
    arc_fortnox_backoff_minutes = fields.Char(
        string="Fortnox Backoff Schedule (min)",
        default="5,15,60,240,1440",
        help="Comma separated minutes between retries. The first value is "
        "the delay after the first failed attempt.",
    )
    arc_fortnox_max_attempts = fields.Integer(
        string="Fortnox Max Attempts",
        default=5,
        help="The attempt number at which a queue entry becomes a hard "
        "error and raises an activity on the order.",
    )
    arc_fortnox_max_description_length = fields.Integer(
        string="Fortnox Max Description Length",
        default=0,
        help="Invoice row descriptions longer than this continue on "
        "continuation rows with zero price and quantity. 0 means no "
        "splitting. Keep 0 until the Fortnox limit is verified.",
    )
    arc_fortnox_send_article_numbers = fields.Boolean(
        string="Fortnox: Send Article Numbers",
        default=False,
        help="When enabled, a product with a Fortnox article number "
        "configured uses ArticleNumber on the invoice row and the article "
        "must exist in Fortnox. Off by default: rows are sent as free text.",
    )

    @api.constrains("arc_fortnox_trigger_domain")
    def _check_arc_fortnox_trigger_domain(self):
        for company in self:
            raw = (company.arc_fortnox_trigger_domain or "").strip()
            if not raw:
                continue
            try:
                domain = ast.literal_eval(raw)
            except (ValueError, SyntaxError) as error:
                raise ValidationError(
                    _(
                        "The Fortnox trigger domain is not a valid Python "
                        "domain: %s",
                        error,
                    )
                ) from error
            if not isinstance(domain, list):
                raise ValidationError(
                    _("The Fortnox trigger domain must be a Python list.")
                )
            try:
                self.env["sale.order"].search(domain, limit=1)
            except (ValueError, TypeError) as error:
                raise ValidationError(
                    _(
                        "The Fortnox trigger domain is not valid for "
                        "sale.order: %s",
                        error,
                    )
                ) from error

    @api.constrains("arc_fortnox_backoff_minutes")
    def _check_arc_fortnox_backoff_minutes(self):
        for company in self:
            raw = (company.arc_fortnox_backoff_minutes or "").strip()
            if not raw:
                raise ValidationError(
                    _("The Fortnox backoff schedule cannot be empty.")
                )
            try:
                values = [
                    int(part.strip())
                    for part in raw.split(",")
                    if part.strip()
                ]
            except ValueError as error:
                raise ValidationError(
                    _(
                        "The Fortnox backoff schedule must be comma "
                        "separated whole minutes: %s",
                        error,
                    )
                ) from error
            if not values or any(value <= 0 for value in values):
                raise ValidationError(
                    _(
                        "The Fortnox backoff schedule must contain positive "
                        "whole minutes."
                    )
                )

    @api.constrains(
        "arc_fortnox_throttle_calls",
        "arc_fortnox_throttle_seconds",
        "arc_fortnox_timeout_connect",
        "arc_fortnox_timeout_read",
        "arc_fortnox_max_attempts",
        "arc_fortnox_max_description_length",
    )
    def _check_arc_fortnox_positive_settings(self):
        for company in self:
            if company.arc_fortnox_throttle_calls <= 0:
                raise ValidationError(
                    _("Fortnox calls per period must be positive.")
                )
            if company.arc_fortnox_throttle_seconds <= 0:
                raise ValidationError(
                    _("The Fortnox throttle period must be positive.")
                )
            if company.arc_fortnox_timeout_connect <= 0:
                raise ValidationError(
                    _("The Fortnox connect timeout must be positive.")
                )
            if company.arc_fortnox_timeout_read <= 0:
                raise ValidationError(
                    _("The Fortnox response timeout must be positive.")
                )
            if not 1 <= company.arc_fortnox_max_attempts <= 10:
                raise ValidationError(
                    _("Fortnox max attempts must be between 1 and 10.")
                )
            if company.arc_fortnox_max_description_length < 0:
                raise ValidationError(
                    _("The Fortnox max description length cannot be negative.")
                )

    def _arc_fortnox_env_param(self, *names):
        """Read a parameter from ir.config_parameter, then from odoo.conf."""
        parameters = self.env["ir.config_parameter"].sudo()
        for name in names:
            value = parameters.get_param(name)
            if value:
                return value
        for name in names:
            value = odoo_config.get(name)
            if value:
                return str(value)
        return False

    def arc_fortnox_client_credentials(self):
        """Return (client_id, client_secret) for this company.

        Resolution order: the company settings, then the parameters
        arc_fortnox.client_id and arc_fortnox.client_secret (underscore
        variants accepted) in ir.config_parameter or odoo.conf.
        """
        self.ensure_one()
        client_id = self.arc_fortnox_client_id or self._arc_fortnox_env_param(
            *CLIENT_ID_PARAM_NAMES
        )
        client_secret = (
            self.arc_fortnox_client_secret
            or self._arc_fortnox_env_param(*CLIENT_SECRET_PARAM_NAMES)
        )
        if not client_id or not client_secret:
            raise UserError(
                _(
                    "No Fortnox client credentials for company %s. Set the "
                    "client id and secret under Settings, or provide the "
                    "parameters arc_fortnox.client_id and "
                    "arc_fortnox.client_secret in the server configuration.",
                    self.name,
                )
            )
        return client_id, client_secret

    def arc_fortnox_backoff_delays(self):
        """Return the backoff schedule as a list of minute values."""
        self.ensure_one()
        raw = self.arc_fortnox_backoff_minutes or ""
        return [int(part.strip()) for part in raw.split(",") if part.strip()]

    def arc_fortnox_client(self):
        """Return a configured Fortnox API client for this company.

        The credential row is created on demand through sudo(); the client
        itself never exposes tokens to the caller.
        """
        self.ensure_one()
        credential = self.env["arc.fortnox.credential"]._arc_fortnox_ensure(self)
        return FortnoxClient(self, credential)

    def _arc_fortnox_redirect_uri(self):
        """The fixed OAuth redirect URI for this installation.

        The parameter arc_fortnox.redirect_uri wins when set. Otherwise the
        URI derives from web.base.url, which Odoo rewrites when an
        administrator logs in unless web.base.url.freeze is set; a changed
        value no longer matches the app registration at Fortnox.
        """
        self.ensure_one()
        parameters = self.env["ir.config_parameter"].sudo()
        fixed = (parameters.get_param(REDIRECT_URI_PARAM) or "").strip()
        if fixed:
            return fixed
        base = parameters.get_param("web.base.url", "").rstrip("/")
        return f"{base}/arc_fortnox/callback"

    def arc_fortnox_authorize_url(self):
        """Build the Fortnox authorization URL and arm the state nonce.

        The nonce is stored on the credential, single use, valid 10
        minutes, compared in constant time at the callback. The service
        account mode is requested so the connection does not depend on one
        person's login; Fortnox ignores the parameter where unsupported.
        """
        self.ensure_one()
        client_id, _client_secret = self.arc_fortnox_client_credentials()
        credential = self.env["arc.fortnox.credential"]._arc_fortnox_ensure(self)
        nonce = secrets.token_urlsafe(32)
        credential.sudo().write(
            {
                "state": "authorizing",
                "state_nonce": nonce,
                "state_nonce_expires_at": fields.Datetime.now()
                + timedelta(minutes=10),
            }
        )
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": self._arc_fortnox_redirect_uri(),
                "scope": AUTHORIZE_SCOPE,
                "state": nonce,
                "response_type": "code",
                "access_type": "offline",
                "account_type": "service",
            }
        )
        return f"{OAUTH_AUTHORIZE_URL}?{query}"
