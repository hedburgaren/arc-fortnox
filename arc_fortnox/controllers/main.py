from odoo import http
from odoo.http import request
from odoo.tools.translate import _

from ..models.fortnox_client import FortnoxAuthError


class FortnoxAuthController(http.Controller):
    @http.route(
        "/arc_fortnox/callback",
        type="http",
        auth="user",
        website=False,
        csrf=False,
    )
    def arc_fortnox_callback(self, **params):
        """OAuth2 callback.

        auth='user' requires a login and the handler refuses everyone
        outside base.group_system. The state nonce is verified server side
        in the credential model: unknown, reused or expired states are
        rejected without leaking which failed.
        """
        if not request.env.user.has_group("base.group_system"):
            return request.make_response(
                _("Not allowed."), status=403, content_type="text/plain"
            )
        try:
            request.env["arc.fortnox.credential"]._arc_fortnox_handle_callback(
                state=params.get("state") or "",
                code=params.get("code") or "",
                error=params.get("error"),
            )
        except FortnoxAuthError as error:
            return request.make_response(
                _("Authorization failed: %s", error.error_text),
                status=400,
                content_type="text/plain",
            )
        return request.redirect(
            "/web#action=&model=ir.module.category",
            localize=False,
        )
