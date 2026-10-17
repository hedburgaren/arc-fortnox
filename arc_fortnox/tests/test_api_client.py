import json as jsonlib
from unittest.mock import patch

import requests

from odoo.addons.arc_fortnox.models.fortnox_client import (
    FortnoxAuthError,
    FortnoxClient,
    FortnoxError,
    _throttle_windows,
)
from odoo.exceptions import AccessError

from .common import ArcFortnoxF1Common


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        payload = body if body is not None else {}
        self.content = jsonlib.dumps(payload).encode("utf-8")

    def json(self):
        return jsonlib.loads(self.content)


class TestFortnoxClient(ArcFortnoxF1Common):
    def _make_client(self):
        return FortnoxClient(
            self.company,
            self.credential,
            clock=lambda: self.now[0],
            sleep=lambda seconds: (
                self.sleeps.append(seconds),
                self.now.__setitem__(0, self.now[0] + seconds),
            ),
        )

    def setUp(self):
        super().setUp()
        self.credential = self.env["arc.fortnox.credential"].create(
            {
                "company_id": self.company.id,
                "state": "authorized",
                "access_token": "test-access-token",
            }
        )
        self.company.write(
            {
                "arc_fortnox_throttle_calls": 25,
                "arc_fortnox_throttle_seconds": 5,
                "arc_fortnox_timeout_connect": 10,
                "arc_fortnox_timeout_read": 30,
            }
        )
        _throttle_windows.clear()
        self.now = [100.0]
        self.sleeps = []
        self.client = self._make_client()

    def _stub_transport(self, client, responses):
        """Queue FakeResponse objects or exceptions per request.

        Patches the transport on the given client instance.
        """
        calls = []

        def fake_transport(method, url, headers, payload, params):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "payload": payload,
                    "params": params,
                }
            )
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        patcher = patch.object(client, "_request_raw", fake_transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_get_sends_bearer_token_and_builds_url(self):
        calls = self._stub_transport(self.client, [FakeResponse(200, {"Customer": {}})])
        result = self.client.get("customers/1")
        self.assertEqual(result, {"Customer": {}})
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(
            calls[0]["url"], "https://api.fortnox.se/3/customers/1"
        )
        self.assertEqual(
            calls[0]["headers"]["Authorization"], "Bearer test-access-token"
        )
        self.assertEqual(calls[0]["headers"]["Accept"], "application/json")

    def test_post_sends_json_payload(self):
        calls = self._stub_transport(self.client, [FakeResponse(201, {"Invoice": {}})])
        self.client.post("invoices", {"Invoice": {"CustomerNumber": "1"}})
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(
            calls[0]["payload"], {"Invoice": {"CustomerNumber": "1"}}
        )

    def test_error_information_mapped(self):
        self._stub_transport(self.client, [
                FakeResponse(
                    404,
                    {
                        "ErrorInformation": {
                            "error": "2000410",
                            "message": "Customer not found",
                        }
                    },
                )
            ]
        )
        with self.assertRaises(FortnoxError) as context:
            self.client.get("customers/999")
        self.assertEqual(context.exception.error_code, "2000410")
        self.assertEqual(context.exception.error_text, "Customer not found")
        self.assertEqual(context.exception.http_status, 404)

    def test_timeout_mapped(self):
        self._stub_transport(self.client, [requests.exceptions.Timeout()])
        with self.assertRaises(FortnoxError) as context:
            self.client.get("customers")
        self.assertEqual(context.exception.error_code, "timeout")

    def test_connection_error_mapped(self):
        self._stub_transport(self.client, [requests.exceptions.ConnectionError()])
        with self.assertRaises(FortnoxError) as context:
            self.client.get("customers")
        self.assertEqual(context.exception.error_code, "connection_error")

    def test_unauthorized_drops_token_and_raises(self):
        self._stub_transport(self.client, [FakeResponse(401, {"ErrorInformation": {}})])
        # try/except on purpose: Odoo's assertRaises wraps the block in a
        # savepoint that rolls back, which would undo the token drop we are
        # asserting on. FortnoxAuthError is not a database error, so the
        # transaction stays perfectly usable here.
        try:
            self.client.get("customers")
        except FortnoxAuthError:
            pass
        else:
            self.fail("FortnoxAuthError was not raised.")
        fresh = self.env["arc.fortnox.credential"].search(
            [("company_id", "=", self.company.id)]
        )
        self.assertEqual(fresh.state, "not_authorized")
        self.assertFalse(fresh.access_token)

    def test_not_authorized_client_raises_before_any_call(self):
        self.credential.write({"access_token": False})
        with self.assertRaises(FortnoxAuthError):
            self.client.get("customers")

    def test_throttle_sleeps_at_window_limit(self):
        self.company.write(
            {"arc_fortnox_throttle_calls": 2, "arc_fortnox_throttle_seconds": 5}
        )
        client = self._make_client()
        self._stub_transport(client, [
                FakeResponse(200, {}),
                FakeResponse(200, {}),
                FakeResponse(200, {}),
            ]
        )
        client.get("customers/1")
        client.get("customers/2")
        self.assertEqual(self.sleeps, [])
        client.get("customers/3")
        self.assertTrue(
            self.sleeps, "The third call inside the window must wait."
        )
        self.assertGreaterEqual(self.now[0], 105.0)

    def test_throttle_window_drains_over_time(self):
        self.company.write(
            {"arc_fortnox_throttle_calls": 1, "arc_fortnox_throttle_seconds": 5}
        )
        client = self._make_client()
        self._stub_transport(client, [FakeResponse(200, {}), FakeResponse(200, {})]
        )
        client.get("customers/1")
        self.now[0] += 6.0
        client.get("customers/2")
        self.assertEqual(self.sleeps, [])

    def test_client_factory_from_company(self):
        client = self.company.arc_fortnox_client()
        self.assertIsInstance(client, FortnoxClient)
        self.assertEqual(client._access_token, "test-access-token")

    def test_client_factory_does_not_leak_tokens_to_plain_users(self):
        plain = self._create_plain_user()
        with self.assertRaises(AccessError):
            self.env["arc.fortnox.credential"].with_user(plain).search([])
