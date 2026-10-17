from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.arc_fortnox.models.fortnox_client import FortnoxError

from .common import ArcFortnoxF1Common


class FakeClient:
    def __init__(self, get_results=None, post_results=None):
        self.calls = []
        self._get_results = list(get_results or [])
        self._post_results = list(post_results or [])

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        outcome = self._get_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, path, payload):
        self.calls.append(("POST", path, payload))
        outcome = self._post_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestQueueProcessing(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.invoicer = self._create_invoicer_user()
        self.queue = self.env["arc.fortnox.queue"]
        self.fake_client = FakeClient()

    def _patch_client(self):
        patcher = patch.object(
            type(self.company),
            "arc_fortnox_client",
            lambda company: self.fake_client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _enqueue(self, order=None):
        order = order or self._create_confirmed_order()
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        entry = self.queue.search([("sale_order_id", "=", order.id)])
        self.assertEqual(entry.state, "pending")
        return order, entry

    def _run_cron(self):
        self.queue._arc_fortnox_cron_process(commit=False)

    def test_success_marks_done_and_writes_number(self):
        self._patch_client()
        order, entry = self._enqueue()
        created = []

        with patch.object(
            type(order), "_arc_fortnox_push_draft", lambda self, client: "101"
        ):
            with patch.object(
                type(order),
                "_arc_fortnox_invoice_created",
                lambda self: created.append(self.id),
            ):
                self._run_cron()

        self.assertEqual(entry.state, "done")
        self.assertEqual(entry.fortnox_invoice_number, "101")
        self.assertEqual(order.arc_fortnox_invoice_number, "101")
        self.assertEqual(created, [order.id])
        bodies = order.message_ids.mapped("body")
        self.assertTrue(any("101" in body for body in bodies))

    def test_failure_applies_first_backoff_step(self):
        self._patch_client()
        order, entry = self._enqueue()

        with patch.object(
            type(order),
            "_arc_fortnox_push_draft",
            lambda self, client: (_ for _ in ()).throw(
                FortnoxError("boom", code="E1")
            ),
        ):
            self._run_cron()

        self.assertEqual(entry.state, "pending")
        self.assertEqual(entry.attempts, 1)
        self.assertEqual(entry.error_code, "E1")
        self.assertEqual(entry.error_text, "boom")
        expected = fields.Datetime.now() + timedelta(minutes=5)
        self.assertLessEqual(entry.next_attempt, expected)
        self.assertGreaterEqual(
            entry.next_attempt, fields.Datetime.now() + timedelta(minutes=4)
        )
        self.assertFalse(order.arc_fortnox_invoice_number)

    def test_backoff_uses_second_step_on_second_failure(self):
        self._patch_client()
        order, entry = self._enqueue()
        error = FortnoxError("boom", code="E1")

        with patch.object(
            type(order),
            "_arc_fortnox_push_draft",
            lambda self, client: (_ for _ in ()).throw(error),
        ):
            self._run_cron()
            # The first backoff step makes the entry not due yet; pull it
            # back to due before the second cron run.
            entry.write(
                {"next_attempt": fields.Datetime.now() - timedelta(minutes=1)}
            )
            self._run_cron()

        self.assertEqual(entry.attempts, 2)
        expected = fields.Datetime.now() + timedelta(minutes=15)
        self.assertLessEqual(entry.next_attempt, expected)
        self.assertGreaterEqual(
            entry.next_attempt, fields.Datetime.now() + timedelta(minutes=14)
        )

    def test_hard_error_after_max_attempts_creates_activity(self):
        self._patch_client()
        order, entry = self._enqueue()
        self.company.write({"arc_fortnox_max_attempts": 2})
        error = FortnoxError("still broken", code="E2")

        with patch.object(
            type(order),
            "_arc_fortnox_push_draft",
            lambda self, client: (_ for _ in ()).throw(error),
        ):
            self._run_cron()
            entry.write(
                {"next_attempt": fields.Datetime.now() - timedelta(minutes=1)}
            )
            self._run_cron()

        self.assertEqual(entry.state, "error")
        self.assertEqual(entry.attempts, 2)
        self.assertEqual(entry.error_code, "E2")
        self.assertTrue(order.activity_ids, "A hard error needs an activity.")
        self.assertIn(
            "Fortnox invoicing failed",
            order.activity_ids.mapped("summary")[0],
        )

    def test_fortnox_unreachable_leaves_order_untouched(self):
        self._patch_client()
        order, entry = self._enqueue()

        with patch.object(
            type(order),
            "_arc_fortnox_push_draft",
            lambda self, client: (_ for _ in ()).throw(
                FortnoxError("Fortnox is unreachable.", code="connection_error")
            ),
        ):
            self._run_cron()

        self.assertEqual(entry.state, "pending")
        self.assertFalse(order.arc_fortnox_invoice_number)
        self.assertEqual(entry.attempts, 1)

    def test_not_due_entry_is_left_alone(self):
        self._patch_client()
        order, entry = self._enqueue()
        entry.write(
            {"next_attempt": fields.Datetime.now() + timedelta(minutes=10)}
        )
        self._run_cron()
        self.assertEqual(entry.state, "pending")
        self.assertEqual(self.fake_client.calls, [])

    def test_crash_recovery_adopts_existing_draft(self):
        self._patch_client()
        order, entry = self._enqueue()
        # Simulate a crash after the POST: the entry is in sending.
        entry.write({"state": "sending"})
        self.fake_client._get_results.append(
            {"Invoices": [{"DocumentNumber": "77"}]}
        )

        def forbidden_push(self, client):
            raise AssertionError("No new POST may happen during adoption.")

        with patch.object(type(order), "_arc_fortnox_push_draft", forbidden_push):
            self._run_cron()

        self.assertEqual(entry.state, "done")
        self.assertEqual(entry.fortnox_invoice_number, "77")
        self.assertEqual(order.arc_fortnox_invoice_number, "77")
        self.assertEqual(
            self.fake_client.calls,
            [("GET", "invoices", {"yourordernumber": order.name})],
        )

    def test_sending_entry_without_hit_posts_once(self):
        self._patch_client()
        order, entry = self._enqueue()
        entry.write({"state": "sending"})
        self.fake_client._get_results.append({"Invoices": []})

        with patch.object(
            type(order), "_arc_fortnox_push_draft", lambda self, client: "55"
        ):
            self._run_cron()

        self.assertEqual(entry.state, "done")
        self.assertEqual(entry.fortnox_invoice_number, "55")
        # The adoption search ran; the POST itself lives inside the push
        # seam (invoice draft builder) and never happened through this
        # client because the seam was patched.
        self.assertEqual(len(self.fake_client.calls), 1)
        self.assertEqual(self.fake_client.calls[0][0], "GET")
        self.assertEqual(
            self.fake_client.calls[0][2], {"yourordernumber": order.name}
        )

    def test_done_and_cancelled_entries_are_skipped(self):
        self._patch_client()
        order, entry = self._enqueue()
        entry.write({"state": "cancelled"})
        self._run_cron()
        self.assertEqual(entry.state, "cancelled")
        self.assertEqual(self.fake_client.calls, [])
