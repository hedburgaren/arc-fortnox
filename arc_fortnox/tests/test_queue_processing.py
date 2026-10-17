from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tools import mute_logger
from odoo.addons.arc_fortnox.models.fortnox_client import FortnoxError

from .common import ArcFortnoxF1Common


class FakeClient:
    def __init__(self, get_results=None, post_results=None):
        self.calls = []
        self._get_results = list(get_results or [])
        self._post_results = list(post_results or [])

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "invoices" and not self._get_results:
            # The draft search before every post: nothing found.
            return {"Invoices": []}
        if path == "customers" and not self._get_results:
            return {"Customers": []}
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

    def test_database_error_in_one_entry_does_not_stop_the_run(self):
        # Verifier D-10: one savepoint per entry, not only per step.
        self._patch_client()
        order_a, entry_a = self._enqueue()
        order_b, entry_b = self._enqueue()

        def push(order, client):
            if order == order_a:
                order.env.cr.execute("SELECT 1/0")
            return "102"

        with patch.object(type(order_a), "_arc_fortnox_push_draft", push), mute_logger(
            "odoo.addons.arc_fortnox.models.arc_fortnox_queue", "odoo.sql_db"
        ):
            self._run_cron()
        self.env.flush_all()

        self.assertEqual(entry_a.state, "pending")
        self.assertEqual(entry_a.error_code, "unexpected_error")
        self.assertEqual(entry_a.attempts, 1)
        self.assertFalse(order_a.arc_fortnox_invoice_number)
        self.assertEqual(entry_b.state, "done")
        self.assertEqual(order_b.arc_fortnox_invoice_number, "102")

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
            {
                "Invoices": [
                    {
                        "DocumentNumber": "77",
                        "ExternalInvoiceReference1": order.name,
                        "Cancelled": False,
                    }
                ]
            }
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
            [("GET", "invoices", {"externalinvoicereference1": order.name})],
        )

    def test_sending_entry_without_hit_posts_once(self):
        self._patch_client()
        order, entry = self._enqueue()
        entry.write({"state": "sending"})
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
            self.fake_client.calls[0][2], {"externalinvoicereference1": order.name}
        )

    def test_done_and_cancelled_entries_are_skipped(self):
        self._patch_client()
        order, entry = self._enqueue()
        entry.write({"state": "cancelled"})
        self._run_cron()
        self.assertEqual(entry.state, "cancelled")
        self.assertEqual(self.fake_client.calls, [])

    def _draft_hit(self, order, number, cancelled=False, reference=None):
        return {
            "DocumentNumber": number,
            "ExternalInvoiceReference1": order.name if reference is None else reference,
            "Cancelled": cancelled,
        }

    def test_timeout_after_post_adopts_instead_of_posting_again(self):
        # Audit P1: Fortnox accepted the draft but the answer timed out.
        self._patch_client()
        order, entry = self._enqueue()
        posts = []

        def push_times_out(self, client):
            posts.append(self.id)
            raise FortnoxError("timeout", code="timeout")

        with patch.object(type(order), "_arc_fortnox_push_draft", push_times_out):
            self._run_cron()
        self.assertEqual(entry.state, "pending")
        entry.write({"next_attempt": fields.Datetime.now() - timedelta(minutes=1)})
        self.fake_client._get_results.append(
            {"Invoices": [self._draft_hit(order, "88")]}
        )
        with patch.object(type(order), "_arc_fortnox_push_draft", push_times_out):
            self._run_cron()
        self.assertEqual(len(posts), 1, "The retry must adopt, not post again.")
        self.assertEqual(entry.state, "done")
        self.assertEqual(order.arc_fortnox_invoice_number, "88")

    def test_hook_failure_after_post_does_not_retry(self):
        # Audit P2: the companion hook breaks after the draft exists.
        self._patch_client()
        order, entry = self._enqueue()

        def broken_hook(self):
            raise RuntimeError("companion hook broke")

        with patch.object(
            type(order), "_arc_fortnox_push_draft", lambda self, client: "12"
        ), patch.object(
            type(order), "_arc_fortnox_invoice_created", broken_hook
        ), mute_logger("odoo.addons.arc_fortnox.models.arc_fortnox_queue"):
            self._run_cron()
        self.assertEqual(entry.state, "done")
        self.assertEqual(order.arc_fortnox_invoice_number, "12")
        self.assertIn(
            "Fortnox follow up failed", order.activity_ids.mapped("summary")
        )

    def test_cancelled_fortnox_invoice_is_not_adopted(self):
        # Audit P3: an old, cancelled draft carries the same reference.
        self._patch_client()
        order, entry = self._enqueue()
        entry.write({"state": "sending"})
        self.fake_client._get_results.append(
            {"Invoices": [self._draft_hit(order, "5", cancelled=True)]}
        )
        with patch.object(
            type(order), "_arc_fortnox_push_draft", lambda self, client: "6"
        ):
            self._run_cron()
        self.assertEqual(order.arc_fortnox_invoice_number, "6")

    def test_reference_must_match_exactly(self):
        self._patch_client()
        order, entry = self._enqueue()
        self.fake_client._get_results.append(
            {"Invoices": [self._draft_hit(order, "5", reference=order.name + "-X")]}
        )
        with patch.object(
            type(order), "_arc_fortnox_push_draft", lambda self, client: "6"
        ):
            self._run_cron()
        self.assertEqual(order.arc_fortnox_invoice_number, "6")

    def test_two_live_drafts_stop_without_posting(self):
        self._patch_client()
        order, entry = self._enqueue()
        self.fake_client._get_results.append(
            {"Invoices": [self._draft_hit(order, "5"), self._draft_hit(order, "7")]}
        )

        def forbidden_push(self, client):
            raise AssertionError("No POST when the reference is ambiguous.")

        with patch.object(type(order), "_arc_fortnox_push_draft", forbidden_push):
            self._run_cron()
        self.assertEqual(entry.error_code, "invoice_reference_ambiguous")
        self.assertFalse(order.arc_fortnox_invoice_number)

    def test_listed_invoice_without_reference_is_read_in_full(self):
        self._patch_client()
        order, entry = self._enqueue()
        self.fake_client._get_results.extend(
            [
                {"Invoices": [{"DocumentNumber": "9"}]},
                {"Invoice": self._draft_hit(order, "9")},
            ]
        )
        self._run_cron()
        self.assertEqual(order.arc_fortnox_invoice_number, "9")
        self.assertEqual(self.fake_client.calls[1][1], "invoices/9")

    def test_order_cancelled_after_enqueue_is_not_sent(self):
        # Audit D-14.
        self._patch_client()
        order, entry = self._enqueue()
        entry.write({"state": "sending"})
        order._action_cancel()
        self.assertEqual(order.state, "cancel")

        def forbidden_push(self, client):
            raise AssertionError("A cancelled order is never sent.")

        with patch.object(type(order), "_arc_fortnox_push_draft", forbidden_push):
            self._run_cron()
        self.assertEqual(entry.state, "cancelled")
        self.assertEqual(self.fake_client.calls, [])

    def test_cancelling_the_order_cancels_pending_entries(self):
        order, entry = self._enqueue()
        order._action_cancel()
        self.assertEqual(entry.state, "cancelled")

    def test_order_with_number_is_not_sent_again(self):
        self._patch_client()
        order, entry = self._enqueue()
        order.sudo().write({"arc_fortnox_invoice_number": "3"})

        def forbidden_push(self, client):
            raise AssertionError("An order with a Fortnox number is never sent again.")

        with patch.object(type(order), "_arc_fortnox_push_draft", forbidden_push):
            self._run_cron()
        self.assertEqual(entry.state, "cancelled")
