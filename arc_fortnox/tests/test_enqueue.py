from unittest.mock import patch

from psycopg2.errors import UniqueViolation

from odoo.exceptions import AccessError, UserError

from .common import ArcFortnoxF1Common


class TestEnqueue(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.invoicer = self._create_invoicer_user()
        self.plain = self._create_plain_user()
        self.queue = self.env["arc.fortnox.queue"]

    def test_enqueue_creates_pending_entry(self):
        order = self._create_confirmed_order()
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        entry = self.queue.search([("sale_order_id", "=", order.id)])
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry.state, "pending")
        self.assertEqual(entry.company_id, self.company)
        self.assertEqual(entry.attempts, 0)

    def test_warnings_ask_before_queueing(self):
        order = self._create_confirmed_order()
        with patch.object(
            type(order),
            "_arc_fortnox_enqueue_warnings",
            lambda self: ["Freight is missing."],
        ):
            action = order.with_user(self.invoicer).action_arc_fortnox_enqueue()
            self.assertEqual(action["res_model"], "arc.fortnox.enqueue.confirm")
            self.assertFalse(self.queue.search([("sale_order_id", "=", order.id)]))
            wizard = (
                self.env["arc.fortnox.enqueue.confirm"]
                .with_user(self.invoicer)
                .browse(action["res_id"])
            )
            self.assertIn("Freight is missing.", wizard.warning_text)
            self.assertIn(order.name, wizard.warning_text)
            wizard.action_confirm()
        entry = self.queue.search([("sale_order_id", "=", order.id)])
        self.assertEqual(entry.state, "pending")

    def test_enqueue_requires_invoicer_group(self):
        order = self._create_confirmed_order()
        with self.assertRaises(AccessError):
            order.with_user(self.plain).action_arc_fortnox_enqueue()
        self.assertFalse(
            self.queue.search([("sale_order_id", "=", order.id)])
        )

    def test_enqueue_refuses_unconfirmed_order(self):
        product = self.env["product.product"].create(
            {"name": "Draft Only Product", "list_price": 10.0}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": self.env["res.partner"]
                .create({"name": "Draft Partner"})
                .id,
                "order_line": [
                    (0, 0, {"product_id": product.id, "product_uom_qty": 1})
                ],
            }
        )
        with self.assertRaises(UserError):
            order.with_user(self.invoicer).action_arc_fortnox_enqueue()

    def test_enqueue_refuses_order_with_invoice_number(self):
        order = self._create_confirmed_order()
        order.sudo().write({"arc_fortnox_invoice_number": "101"})
        with self.assertRaises(UserError):
            order.with_user(self.invoicer).action_arc_fortnox_enqueue()

    def test_enqueue_refuses_duplicate_active_entry(self):
        order = self._create_confirmed_order()
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        with self.assertRaises(UserError):
            order.with_user(self.invoicer).action_arc_fortnox_enqueue()

    def test_enqueue_allowed_hook_can_refuse(self):
        order = self._create_confirmed_order()
        with patch.object(
            type(order), "_arc_fortnox_enqueue_allowed", lambda self: False
        ):
            with self.assertRaises(UserError):
                order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        # Outside the patch the order queues normally.
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        entry = self.queue.search([("sale_order_id", "=", order.id)])
        self.assertEqual(entry.state, "pending")

    def test_chatter_note_on_enqueue(self):
        order = self._create_confirmed_order()
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        bodies = order.message_ids.mapped("body")
        self.assertTrue(
            any("Fortnox" in body for body in bodies),
            "The enqueue action must leave a note in the chatter.",
        )

    def test_cancel_pending_entry(self):
        order = self._create_confirmed_order()
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        entry = self.queue.search([("sale_order_id", "=", order.id)])
        entry.with_user(self.invoicer).action_cancel()
        self.assertEqual(entry.state, "cancelled")
        # A cancelled entry does not block a new enqueue.
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        entries = self.queue.search([("sale_order_id", "=", order.id)])
        self.assertEqual(len(entries), 2)
        self.assertTrue(
            any(state == "pending" for state in entries.mapped("state"))
        )

    def test_cancel_refuses_non_pending(self):
        order = self._create_confirmed_order()
        entry = self.queue.create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )
        entry.write({"state": "done"})
        with self.assertRaises(UserError):
            entry.with_user(self.invoicer).action_cancel()

    def test_partial_unique_index_blocks_second_active_entry(self):
        order = self._create_confirmed_order()
        self.queue.create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )
        self.env.flush_all()
        with self.assertRaises(UniqueViolation):
            with self.env.cr.savepoint():
                self.queue.create(
                    {"company_id": self.company.id, "sale_order_id": order.id}
                )
        # Done entries do not block a new one.
        done = self.queue.search([("sale_order_id", "=", order.id)])
        done.write({"state": "done"})
        self.env.flush_all()
        self.queue.create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )


class TestQueueDisplay(ArcFortnoxF1Common):
    def test_display_name_contains_order_and_state(self):
        order = self._create_confirmed_order()
        entry = self.env["arc.fortnox.queue"].create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )
        self.assertIn(order.name, entry.display_name)
        self.assertIn("Pending", entry.display_name)

    def test_active_queue_count_field(self):
        order = self._create_confirmed_order()
        self.assertEqual(order.arc_fortnox_active_queue_count, 0)
        self.env["arc.fortnox.queue"].create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )
        order.invalidate_recordset(["arc_fortnox_active_queue_count"])
        self.assertEqual(order.arc_fortnox_active_queue_count, 1)


class TestQueueActions(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.invoicer = self._create_invoicer_user()
        self.plain = self._create_plain_user()
        self.queue = self.env["arc.fortnox.queue"]

    def test_retry_failed_entry(self):
        # Audit D-5: a failed entry has a way out.
        order = self._create_confirmed_order()
        entry = self.queue.create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )
        entry.write({"state": "error", "attempts": 5})
        entry.with_user(self.invoicer).action_retry()
        self.assertEqual(entry.state, "pending")
        self.assertEqual(entry.attempts, 0)

    def test_cancel_failed_entry(self):
        order = self._create_confirmed_order()
        entry = self.queue.create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )
        entry.write({"state": "error"})
        entry.with_user(self.invoicer).action_cancel()
        self.assertEqual(entry.state, "cancelled")

    def test_invoicer_cannot_write_or_create_queue_entries(self):
        # Audit P10 and D-6: state changes only through the actions.
        order = self._create_confirmed_order()
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        entry = self.queue.search([("sale_order_id", "=", order.id)])
        with self.assertRaises(AccessError):
            entry.with_user(self.invoicer).write(
                {"state": "done", "fortnox_invoice_number": "999"}
            )
        with self.assertRaises(AccessError):
            self.queue.with_user(self.invoicer).create(
                {"company_id": self.company.id, "sale_order_id": order.id}
            )
        with self.assertRaises(AccessError):
            entry.with_user(self.invoicer).unlink()

    def test_plain_user_cannot_read_the_queue(self):
        plain = self.plain
        order = self._create_confirmed_order()
        order.with_user(self.invoicer).action_arc_fortnox_enqueue()
        with self.assertRaises(AccessError):
            self.queue.with_user(plain).search([]).read(["state"])

    def test_plain_user_cannot_retry(self):
        plain = self.plain
        order = self._create_confirmed_order()
        entry = self.queue.create(
            {"company_id": self.company.id, "sale_order_id": order.id}
        )
        entry.write({"state": "error"})
        with self.assertRaises(AccessError):
            entry.with_user(plain).action_retry()

    def test_invoicer_cannot_change_the_vat_mapping(self):
        position = self.env["account.fiscal.position"].create(
            {"name": "Mapping ACL", "company_id": self.company.id}
        )
        mapping = self.env["arc.fortnox.vat.mapping"].create(
            {"company_id": self.company.id, "fiscal_position_id": position.id, "vat_type": "SEVAT"}
        )
        mapping.with_user(self.invoicer).read(["vat_type"])
        with self.assertRaises(AccessError):
            mapping.with_user(self.invoicer).write({"vat_type": "EXPORT"})
