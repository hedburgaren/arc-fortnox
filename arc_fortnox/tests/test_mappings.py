from odoo.addons.arc_fortnox.models.fortnox_client import FortnoxError
from odoo.exceptions import ValidationError
from unittest.mock import patch

from .common import ArcFortnoxF1Common


class TestVatMapping(ArcFortnoxF1Common):
    def _create_mapping(self, **values):
        product = self.env["product.product"].create(
            {"name": "Mapping Test Product", "list_price": 100.0}
        )
        partner = self.env["res.partner"].create(
            {"name": "Mapping Test Partner"}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "order_line": [
                    (0, 0, {"product_id": product.id, "product_uom_qty": 1})
                ],
            }
        )
        order.action_confirm()
        fiscal_position = self.env["account.fiscal.position"].create(
            {"name": "Domestic", "company_id": self.company.id}
        )
        order.write({"fiscal_position_id": fiscal_position.id})
        self.mapping = self.env["arc.fortnox.vat.mapping"].create(
            dict(
                {
                    "company_id": self.company.id,
                    "fiscal_position_id": fiscal_position.id,
                    "vat_type": "SEVAT",
                    "revenue_account": "3011",
                },
                **values,
            )
        )
        return order

    def test_vat_mapping_resolution(self):
        order = self._create_mapping()
        mapping = order._arc_fortnox_vat_mapping()
        self.assertEqual(mapping.vat_type, "SEVAT")
        self.assertEqual(mapping.revenue_account, "3011")

    def test_unmapped_fiscal_position_stops(self):
        order = self._create_mapping()
        self.mapping.unlink()
        with self.assertRaises(FortnoxError) as context:
            order._arc_fortnox_vat_mapping()
        self.assertEqual(context.exception.error_code, "vat_mapping_missing")

    def test_revenue_account_must_be_numeric(self):
        with self.assertRaises(ValidationError):
            self.env["arc.fortnox.vat.mapping"].create(
                {
                    "company_id": self.company.id,
                    "fiscal_position_id": self.env[
                        "account.fiscal.position"
                    ].create({"name": "FP Test"}).id,
                    "vat_type": "SEVAT",
                    "revenue_account": "3O11",
                }
            )

    def test_one_mapping_per_fiscal_position(self):
        order = self._create_mapping()
        from psycopg2.errors import UniqueViolation

        with self.assertRaises(UniqueViolation):
            with self.env.cr.savepoint():
                self.env["arc.fortnox.vat.mapping"].create(
                    {
                        "company_id": self.company.id,
                        "fiscal_position_id": order.fiscal_position_id.id,
                        "vat_type": "EXPORT",
                    }
                )


class TestTermAndUnitCodes(ArcFortnoxF1Common):
    def _order_with_term(self, term_code=None):
        term = self.env["account.payment.term"].create(
            {"name": "15 days", "arc_fortnox_code": term_code}
        )
        product = self.env["product.product"].create(
            {"name": "Term Test Product", "list_price": 100.0}
        )
        partner = self.env["res.partner"].create(
            {"name": "Term Test Partner"}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "payment_term_id": term.id,
                "order_line": [
                    (0, 0, {"product_id": product.id, "product_uom_qty": 1})
                ],
            }
        )
        order.action_confirm()
        return order

    def test_payment_term_code_resolution(self):
        order = self._order_with_term("15")
        self.assertEqual(order._arc_fortnox_payment_term_code(), "15")

    def test_unmapped_payment_term_stops(self):
        order = self._order_with_term(None)
        with self.assertRaises(FortnoxError) as context:
            order._arc_fortnox_payment_term_code()
        self.assertEqual(context.exception.error_code, "payment_term_unmapped")

    def test_unmapped_unit_stops(self):
        order = self._order_with_term("15")
        line = order.order_line[0]
        with self.assertRaises(FortnoxError) as context:
            line._arc_fortnox_unit_code()
        self.assertEqual(context.exception.error_code, "unit_unmapped")

    def test_mapped_unit_resolves(self):
        order = self._order_with_term("15")
        line = order.order_line[0]
        line.product_uom.write({"arc_fortnox_unit": "ST"})
        self.assertEqual(line._arc_fortnox_unit_code(), "ST")


class TestAutoEnqueueCron(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.queue = self.env["arc.fortnox.queue"]
        self.invoicer = self._create_invoicer_user()
        self.company.write(
            {
                "arc_fortnox_trigger_mode": "domain",
                "arc_fortnox_trigger_domain": "[('state', '=', 'sale')]",
            }
        )

    def _confirmed_order(self):
        product = self.env["product.product"].create(
            {"name": "Trigger Test Product", "list_price": 10.0}
        )
        partner = self.env["res.partner"].create(
            {"name": "Trigger Test Partner"}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "order_line": [
                    (0, 0, {"product_id": product.id, "product_uom_qty": 1})
                ],
            }
        )
        order.action_confirm()
        return order

    def test_auto_enqueue_enqueues_matching_orders(self):
        order = self._confirmed_order()
        self.env["sale.order"]._arc_fortnox_cron_auto_enqueue()
        entries = self.queue.search([("sale_order_id", "=", order.id)])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries.state, "pending")

    def test_auto_enqueue_skips_orders_with_invoice_number(self):
        order = self._confirmed_order()
        order.sudo().write({"arc_fortnox_invoice_number": "9"})
        self.env["sale.order"]._arc_fortnox_cron_auto_enqueue()
        self.assertFalse(self.queue.search([("sale_order_id", "=", order.id)]))

    def test_auto_enqueue_skips_orders_with_active_entry(self):
        order = self._confirmed_order()
        self.env["sale.order"]._arc_fortnox_cron_auto_enqueue()
        self.env["sale.order"]._arc_fortnox_cron_auto_enqueue()
        entries = self.queue.search([("sale_order_id", "=", order.id)])
        self.assertEqual(len(entries), 1)

    def test_auto_enqueue_respects_hook_refusal(self):
        order = self._confirmed_order()
        with patch.object(
            type(order), "_arc_fortnox_enqueue_allowed", lambda self: False
        ):
            self.env["sale.order"]._arc_fortnox_cron_auto_enqueue()
        self.assertFalse(self.queue.search([("sale_order_id", "=", order.id)]))

    def test_manual_mode_enqueues_nothing(self):
        self.company.write({"arc_fortnox_trigger_mode": "manual"})
        self._confirmed_order()
        self.env["sale.order"]._arc_fortnox_cron_auto_enqueue()
        self.assertFalse(self.queue.search([]))

    def test_domain_mode_with_empty_domain_enqueues_nothing(self):
        self.company.write({"arc_fortnox_trigger_domain": ""})
        self._confirmed_order()
        self.env["sale.order"]._arc_fortnox_cron_auto_enqueue()
        self.assertFalse(self.queue.search([]))
