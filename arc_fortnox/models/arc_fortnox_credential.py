import hmac
import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools.translate import _

from .fortnox_client import FortnoxAuthError, token_request

_logger = logging.getLogger(__name__)

CREDENTIAL_STATES = [
    ("not_authorized", "Not authorized"),
    ("authorizing", "Authorizing"),
    ("authorized", "Authorized"),
    ("error", "Error"),
]

AUTHORIZE_SCOPE = "invoice customer"
REFRESH_WARN_DAYS = 38
REFRESH_STALE_DAYS = 45


class ArcFortnoxCredential(models.Model):
    """One OAuth2 connection row per company.

    The model itself is readable only by base.group_system through the access
    rules, and the token fields carry field level groups as a second layer.
    Module code reads this model with sudo() and never exposes tokens over
    RPC, so a plain user or a worker account gets an access error.
    """

    _name = "arc.fortnox.credential"
    _description = "Fortnox Connection Credential"
    _order = "company_id, id"
    _rec_name = "company_id"

    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        index=True,
        ondelete="cascade",
    )
    state = fields.Selection(
        selection=CREDENTIAL_STATES,
        string="State",
        default="not_authorized",
        required=True,
        index=True,
    )
    scope = fields.Char(
        string="Granted Scopes",
        copy=False,
        help="The scopes Fortnox granted at the last authorization.",
    )
    access_token = fields.Char(copy=False, groups="base.group_system")
    refresh_token = fields.Char(copy=False, groups="base.group_system")
    access_token_expires_at = fields.Datetime(
        string="Access Token Expires At", copy=False
    )
    last_refresh_at = fields.Datetime(string="Last Refresh At", copy=False)
    last_error = fields.Text(string="Last Error", copy=False)
    state_nonce = fields.Char(
        string="Authorization State Nonce",
        copy=False,
        groups="base.group_system",
    )
    state_nonce_expires_at = fields.Datetime(
        string="State Nonce Expires At", copy=False
    )
    expiry_warned_at = fields.Datetime(
        string="Expiry Warned At",
        copy=False,
        help="Set when the admins were warned about an idle connection.",
    )

    _sql_constraints = [
        (
            "company_uniq",
            "unique(company_id)",
            "A company can have only one Fortnox connection.",
        ),
    ]

    @api.model
    def _arc_fortnox_ensure(self, company):
        """Return the credential row for the company, creating it if needed.

        Always called through sudo() from module code.
        """
        self = self.sudo()
        credential = self.search([("company_id", "=", company.id)], limit=1)
        if not credential:
            credential = self.create({"company_id": company.id})
        return credential

    def _arc_fortnox_on_unauthorized(self):
        """Called by the API client when Fortnox rejects the access token.

        Tries a token refresh first; returns True when a new access token
        is in place so the client can retry the request once. Returns False
        when there is nothing to refresh with, leaving the credential in
        not authorized so a new authorization is clearly required.
        """
        self.ensure_one()
        current = self.sudo()
        if not current.refresh_token:
            current.write(
                {
                    "state": "not_authorized",
                    "access_token": False,
                    "refresh_token": False,
                    "access_token_expires_at": False,
                    "last_error": "Fortnox rejected the access token and "
                    "there is no refresh token.",
                }
            )
            return False
        try:
            current._arc_fortnox_refresh_tokens()
            return True
        except FortnoxAuthError as error:
            current.write(
                {
                    "state": "error",
                    "last_error": error.error_text,
                }
            )
            return False

    def _arc_fortnox_store_tokens(self, response):
        """Store a successful token endpoint response on the credential."""
        self.ensure_one()
        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token")
        if not access_token or not refresh_token:
            raise FortnoxAuthError(
                _("The Fortnox token response was incomplete."),
                code="token_response_invalid",
            )
        try:
            expires_in = int(response.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        now = fields.Datetime.now()
        self.sudo().write(
            {
                "state": "authorized",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "access_token_expires_at": now + timedelta(seconds=expires_in),
                "last_refresh_at": now,
                "scope": response.get("scope") or self.scope or AUTHORIZE_SCOPE,
                "last_error": False,
                "expiry_warned_at": False,
            }
        )

    def _arc_fortnox_refresh_tokens(self):
        """Refresh the tokens, with rotation, under a row lock.

        The SELECT FOR UPDATE serializes a cron and a user action, so the
        rotating refresh token can never be burned by two concurrent
        refreshes. Once the lock is held the refresh token is re-read so
        the request carries the latest rotated value.
        """
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM arc_fortnox_credential WHERE id = %s FOR UPDATE",
            [self.id],
        )
        current = self.sudo()
        if not current.refresh_token:
            raise FortnoxAuthError(
                _("The Fortnox connection is not authorized."),
                code="not_authorized",
            )
        company = current.company_id
        client_id, client_secret = company.arc_fortnox_client_credentials()
        response = token_request(
            company,
            {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
            },
            client_id,
            client_secret,
        )
        current._arc_fortnox_store_tokens(response)

    @api.model
    def _arc_fortnox_handle_callback(self, state, code, error):
        """Handle the OAuth callback: verify the state, store the tokens.

        The state nonce is compared in constant time against every pending
        authorization, is single use and expires after 10 minutes. Returns
        the credential that was authorized.
        """
        if error:
            raise FortnoxAuthError(
                _("Fortnox returned an authorization error: %s", error),
                code="authorization_denied",
            )
        match = self.browse()
        now = fields.Datetime.now()
        for candidate in self.sudo().search([("state_nonce", "!=", False)]):
            if not candidate.state_nonce_expires_at:
                continue
            if candidate.state_nonce_expires_at <= now:
                continue
            if hmac.compare_digest(candidate.state_nonce or "", state or ""):
                match = candidate
                break
        if not match:
            raise FortnoxAuthError(
                _(
                    "Unknown, reused or expired authorization state. Start "
                    "a new authorization from the settings."
                ),
                code="state_invalid",
            )
        # Single use: consumed before anything else happens.
        match.write({"state_nonce": False, "state_nonce_expires_at": False})
        company = match.company_id
        client_id, client_secret = company.arc_fortnox_client_credentials()
        response = token_request(
            company,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": company._arc_fortnox_redirect_uri(),
            },
            client_id,
            client_secret,
        )
        match._arc_fortnox_store_tokens(response)
        return match

    @api.model
    def _arc_fortnox_cron_warn_expiry(self):
        """Warn the admins when a connection nears the 45 day idle limit.

        res.company and res.users carry no mail thread in a minimal
        install, so the warning goes out as an email to the system users
        and into the server log. A warned connection is not rewarned until
        the warning is reset by a successful refresh.
        """
        deadline = fields.Datetime.now() - timedelta(days=REFRESH_WARN_DAYS)
        stale = self.sudo().search(
            [
                ("state", "=", "authorized"),
                ("last_refresh_at", "!=", False),
                ("last_refresh_at", "<", deadline),
                ("expiry_warned_at", "=", False),
            ]
        )
        if not stale:
            return True
        admins = (
            self.env["res.users"]
            .sudo()
            .search([("groups_id", "in", self.env.ref("base.group_system").id)])
            .filtered(lambda user: user.email)
        )
        subject = _("Fortnox authorization needs renewal")
        for credential in stale:
            company_names = ", ".join(
                credential.company_id.sudo().mapped("name")
            )
            body = _(
                "The Fortnox connection for %s has not refreshed its token "
                "for more than %s days. Reauthorize within %s days of the "
                "last refresh or the bridge stops: open Settings, Fortnox, "
                "and press Authorize with Fortnox.",
                company_names,
                REFRESH_WARN_DAYS,
                REFRESH_STALE_DAYS,
            )
            if admins:
                self.env["mail.mail"].sudo().create(
                    {
                        "subject": subject,
                        "body_html": f"<p>{body}</p>",
                        "email_to": ", ".join(admins.mapped("email")),
                        "auto_delete": True,
                    }
                ).send()
            _logger.warning(
                "The Fortnox connection for company %s nears its %s day "
                "authorization limit.",
                company_names,
                REFRESH_STALE_DAYS,
            )
            credential.write({"expiry_warned_at": fields.Datetime.now()})
        return True
