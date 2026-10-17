from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.arc_fortnox.models import arc_fortnox_credential as credential_module
from odoo.addons.arc_fortnox.models.fortnox_client import (
    OAUTH_AUTHORIZE_URL,
    FortnoxAuthError,
)

from .common import ArcFortnoxF1Common
from .test_api_client import FakeResponse


class TestOAuthFlow(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.company.write(
            {
                "arc_fortnox_client_id": "test-client-id",
                "arc_fortnox_client_secret": "test-client-secret",
            }
        )
        self.credential = self.env["arc.fortnox.credential"].create(
            {"company_id": self.company.id, "state": "not_authorized"}
        )
        self.token_calls = []
        self.token_results = []

        def fake_token_request(company, data, client_id, client_secret):
            self.token_calls.append((company.id, dict(data)))
            outcome = self.token_results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        patcher = patch.object(
            credential_module, "token_request", fake_token_request
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _token_body(self, **overrides):
        body = {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_in": 3600,
            "scope": "invoice customer",
        }
        body.update(overrides)
        return body

    def test_authorize_url_arms_single_use_nonce(self):
        url = self.company.arc_fortnox_authorize_url()
        self.assertTrue(url.startswith(OAUTH_AUTHORIZE_URL))
        self.assertIn("client_id=test-client-id", url)
        self.assertIn("account_type=service", url)
        self.assertIn("scope=invoice+customer", url)
        self.assertIn("response_type=code", url)
        self.assertIn("access_type=offline", url)
        self.credential.invalidate_recordset()
        self.assertTrue(self.credential.state_nonce)
        self.assertIn(self.credential.state_nonce, url)
        self.assertEqual(self.credential.state, "authorizing")
        self.assertLessEqual(
            self.credential.state_nonce_expires_at,
            fields.Datetime.now() + timedelta(minutes=10, seconds=5),
        )

    def test_callback_stores_tokens_and_consumes_nonce(self):
        self.company.arc_fortnox_authorize_url()
        self.credential.invalidate_recordset()
        nonce = self.credential.state_nonce
        self.token_results.append(self._token_body())
        match = self.env["arc.fortnox.credential"]._arc_fortnox_handle_callback(
            state=nonce, code="auth-code", error=None
        )
        self.assertEqual(match, self.credential)
        self.credential.invalidate_recordset()
        self.assertEqual(self.credential.state, "authorized")
        self.assertEqual(self.credential.access_token, "access-1")
        self.assertEqual(self.credential.refresh_token, "refresh-1")
        self.assertFalse(self.credential.state_nonce)
        self.assertEqual(self.token_calls[0][1]["grant_type"], "authorization_code")
        self.assertEqual(self.token_calls[0][1]["code"], "auth-code")
        # Client credentials travel in the Basic header, never in the body.
        self.assertNotIn("client_secret", self.token_calls[0][1])
        self.assertNotIn("client_id", self.token_calls[0][1])

    def test_redirect_uri_parameter_wins_over_base_url(self):
        parameters = self.env["ir.config_parameter"].sudo()
        parameters.set_param("web.base.url", "http://drifted.example")
        self.assertEqual(
            self.company._arc_fortnox_redirect_uri(),
            "http://drifted.example/arc_fortnox/callback",
        )
        parameters.set_param(
            "arc_fortnox.redirect_uri", "https://odoo.example/arc_fortnox/callback"
        )
        self.assertEqual(
            self.company._arc_fortnox_redirect_uri(),
            "https://odoo.example/arc_fortnox/callback",
        )

    def test_callback_rejects_wrong_state(self):
        self.company.arc_fortnox_authorize_url()
        self.credential.invalidate_recordset()
        with self.assertRaises(FortnoxAuthError) as context:
            self.env["arc.fortnox.credential"]._arc_fortnox_handle_callback(
                state="wrong-state", code="auth-code", error=None
            )
        self.assertEqual(context.exception.error_code, "state_invalid")
        self.assertEqual(self.token_calls, [])

    def test_callback_rejects_reused_state(self):
        self.company.arc_fortnox_authorize_url()
        self.credential.invalidate_recordset()
        nonce = self.credential.state_nonce
        self.token_results.append(self._token_body())
        self.env["arc.fortnox.credential"]._arc_fortnox_handle_callback(
            state=nonce, code="auth-code", error=None
        )
        with self.assertRaises(FortnoxAuthError):
            self.env["arc.fortnox.credential"]._arc_fortnox_handle_callback(
                state=nonce, code="auth-code", error=None
            )
        self.assertEqual(len(self.token_calls), 1)

    def test_callback_rejects_expired_state(self):
        self.company.arc_fortnox_authorize_url()
        self.credential.write(
            {"state_nonce_expires_at": fields.Datetime.now() - timedelta(minutes=1)}
        )
        self.credential.invalidate_recordset()
        with self.assertRaises(FortnoxAuthError):
            self.env["arc.fortnox.credential"]._arc_fortnox_handle_callback(
                state=self.credential.state_nonce,
                code="auth-code",
                error=None,
            )
        self.assertEqual(self.token_calls, [])

    def test_callback_rejects_fortnox_error(self):
        with self.assertRaises(FortnoxAuthError) as context:
            self.env["arc.fortnox.credential"]._arc_fortnox_handle_callback(
                state="anything", code="", error="access_denied"
            )
        self.assertEqual(context.exception.error_code, "authorization_denied")


class TestTokenRefresh(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.company.write(
            {
                "arc_fortnox_client_id": "test-client-id",
                "arc_fortnox_client_secret": "test-client-secret",
            }
        )
        self.credential = self.env["arc.fortnox.credential"].create(
            {
                "company_id": self.company.id,
                "state": "authorized",
                "access_token": "access-old",
                "refresh_token": "refresh-1",
            }
        )
        self.token_calls = []
        self.token_results = []

        def fake_token_request(company, data, client_id, client_secret):
            self.token_calls.append((company.id, dict(data)))
            outcome = self.token_results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        patcher = patch.object(
            credential_module, "token_request", fake_token_request
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_rotates_tokens_in_order(self):
        self.token_results.append(
            {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
            }
        )
        self.credential._arc_fortnox_refresh_tokens()
        self.assertEqual(self.token_calls[0][1]["refresh_token"], "refresh-1")
        self.credential.invalidate_recordset()
        self.assertEqual(self.credential.access_token, "access-2")
        self.assertEqual(self.credential.refresh_token, "refresh-2")
        # The second refresh carries the rotated token: nothing was burned.
        self.token_results.append(
            {
                "access_token": "access-3",
                "refresh_token": "refresh-3",
                "expires_in": 3600,
            }
        )
        self.credential._arc_fortnox_refresh_tokens()
        self.assertEqual(self.token_calls[1][1]["refresh_token"], "refresh-2")

    def test_refresh_takes_a_row_lock(self):
        # The lock is part of the refresh: prove the SQL runs by refreshing
        # inside a savepoint, which would break on a bad statement.
        self.token_results.append(
            {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
            }
        )
        with self.env.cr.savepoint():
            self.credential._arc_fortnox_refresh_tokens()
        self.credential.invalidate_recordset()
        self.assertEqual(self.credential.access_token, "access-2")

    def test_refresh_without_refresh_token_raises(self):
        self.credential.write({"refresh_token": False})
        with self.assertRaises(FortnoxAuthError):
            self.credential._arc_fortnox_refresh_tokens()
        self.assertEqual(self.token_calls, [])

    def test_failed_refresh_leaves_error_state(self):
        self.token_results.append(
            FortnoxAuthError("invalid_grant", code="invalid_grant")
        )
        refreshed = self.credential._arc_fortnox_on_unauthorized()
        self.assertFalse(refreshed)
        self.credential.invalidate_recordset()
        self.assertEqual(self.credential.state, "error")

    def test_client_401_refreshes_and_retries_once(self):
        token_patcher = patch.object(
            credential_module,
            "token_request",
            lambda company, data, client_id, client_secret: {
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_in": 3600,
            },
        )
        token_patcher.start()
        self.addCleanup(token_patcher.stop)

        client = self.company.arc_fortnox_client()
        responses = [
            FakeResponse(401, {"ErrorInformation": {}}),
            FakeResponse(200, {"Customer": {"CustomerNumber": "7"}}),
        ]
        calls = []

        def fake_transport(method, url, headers, payload, params):
            calls.append(headers["Authorization"])
            return responses.pop(0)

        transport_patcher = patch.object(client, "_request_raw", fake_transport)
        transport_patcher.start()
        self.addCleanup(transport_patcher.stop)

        result = client.get("customers/7")
        self.assertEqual(result, {"Customer": {"CustomerNumber": "7"}})
        self.assertEqual(
            calls,
            ["Bearer access-old", "Bearer access-new"],
            "The 401 must trigger one refresh and one retry.",
        )

    def test_client_401_without_refresh_token_raises(self):
        self.credential.write({"refresh_token": False})
        client = self.company.arc_fortnox_client()
        responses = [FakeResponse(401, {"ErrorInformation": {}})]

        def fake_transport(method, url, headers, payload, params):
            return responses.pop(0)

        transport_patcher = patch.object(client, "_request_raw", fake_transport)
        transport_patcher.start()
        self.addCleanup(transport_patcher.stop)

        with self.assertRaises(FortnoxAuthError):
            client.get("customers/7")


class TestExpiryWarning(ArcFortnoxF1Common):
    def _authorized_credential(self, last_refresh_days_ago):
        return self.env["arc.fortnox.credential"].create(
            {
                "company_id": self.company.id,
                "state": "authorized",
                "access_token": "access",
                "refresh_token": "refresh",
                "last_refresh_at": fields.Datetime.now()
                - timedelta(days=last_refresh_days_ago),
            }
        )

    def test_stale_connection_warns_once(self):
        credential = self._authorized_credential(40)
        sent = []
        with patch.object(
            type(self.env["mail.mail"]),
            "send",
            lambda self, **kwargs: sent.append(self.subject),
        ):
            self.env[
                "arc.fortnox.credential"
            ]._arc_fortnox_cron_warn_expiry()
            # The mail is auto deleted after sending, so the call list is
            # the evidence.
            self.assertEqual(
                sent,
                ["Fortnox authorization needs renewal"],
            )
            credential.invalidate_recordset()
            self.assertTrue(credential.expiry_warned_at)
            self.env[
                "arc.fortnox.credential"
            ]._arc_fortnox_cron_warn_expiry()
        self.assertEqual(
            len(sent),
            1,
            "The warning must not duplicate itself.",
        )

    def test_fresh_connection_gets_no_warning(self):
        self._authorized_credential(5)
        sent = []
        with patch.object(
            type(self.env["mail.mail"]),
            "send",
            lambda self, **kwargs: sent.append(self.subject),
        ):
            self.env["arc.fortnox.credential"]._arc_fortnox_cron_warn_expiry()
        self.assertFalse(sent)

    def test_refresh_rearms_the_expiry_warning(self):
        credential = self._authorized_credential(40)
        credential.write({"expiry_warned_at": fields.Datetime.now()})
        credential._arc_fortnox_store_tokens(
            {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
        )
        self.assertFalse(credential.expiry_warned_at)


class TestTokenEndpointRequest(ArcFortnoxF1Common):
    def test_basic_authentication_and_grant_only_body(self):
        from odoo.addons.arc_fortnox.models import fortnox_client

        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured.update(url=url, data=dict(data), headers=dict(headers))
            return FakeResponse(200, {"access_token": "a", "refresh_token": "r"})

        with patch.object(fortnox_client.requests, "post", fake_post):
            fortnox_client.token_request(
                self.company,
                {"grant_type": "refresh_token", "refresh_token": "r0"},
                "the-id",
                "the-secret",
            )
        self.assertEqual(
            captured["headers"]["Authorization"],
            "Basic dGhlLWlkOnRoZS1zZWNyZXQ=",
        )
        self.assertEqual(
            captured["data"], {"grant_type": "refresh_token", "refresh_token": "r0"}
        )
