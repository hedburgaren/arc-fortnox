from unittest.mock import patch

from odoo.addons.arc_fortnox.models.fortnox_client import FortnoxError
from odoo.addons.arc_fortnox.models.sale_order import (
    SaleOrder as SaleOrderModel,
)

from .common import ArcFortnoxF1Common
from .test_queue_processing import FakeClient


class TestCustomerStep(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.fake_client = FakeClient()
        patcher = patch.object(
            type(self.company),
            "arc_fortnox_client",
            lambda company: self.fake_client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.term = self.env["account.payment.term"].create(
            {"name": "15 days", "arc_fortnox_code": "15"}
        )
        self.fiscal_position = self.env["account.fiscal.position"].create(
            {"name": "Domestic", "company_id": self.company.id}
        )
        self.mapping = self.env["arc.fortnox.vat.mapping"].create(
            {
                "company_id": self.company.id,
                "fiscal_position_id": self.fiscal_position.id,
                "vat_type": "SEVAT",
                "revenue_account": "3011",
            }
        )
        self.partner = self.env["res.partner"].create(
            {"name": "Customer Step Partner"}
        )
        product = self.env["product.product"].create(
            {"name": "Customer Step Product", "list_price": 100.0}
        )
        self.order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "payment_term_id": self.term.id,
                "fiscal_position_id": self.fiscal_position.id,
                "order_line": [
                    (
                        0,
                        0,
                        {"product_id": product.id, "product_uom_qty": 1},
                    )
                ],
            }
        )
        self.order.action_confirm()

    def fake_customer(self, **overrides):
        values = {
            "CustomerNumber": "7",
            "VATType": "SEVAT",
            "Currency": self.order.currency_id.name,
            "TermsOfPayment": "15",
        }
        values.update(overrides)
        return values

    def test_normalises_organisation_numbers(self):
        normalise = SaleOrderModel._arc_fortnox_normalise_org_number
        self.assertEqual(normalise("556123-4567"), "5561234567")
        self.assertEqual(normalise("SE5561234567"), "5561234567")
        self.assertEqual(normalise("556123 4567"), "5561234567")
        self.assertEqual(normalise(False), "")

    def test_existing_ref_returns_number_without_touching_fortnox(self):
        self.partner.write({"ref": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer()}
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "7")
        self.assertEqual(self.fake_client.calls[0][0], "GET")
        self.assertEqual(self.fake_client.calls[0][1], "customers/7")

    def test_stale_ref_stops_without_creating(self):
        self.partner.write({"ref": "8"})
        self.fake_client._get_results.append(
            FortnoxError(
                "Customer not found", code="2000410", http_status=404
            )
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_ref_stale")
        self.assertNotIn("POST", [call[0] for call in self.fake_client.calls])

    def test_duplicate_ref_stops(self):
        self.partner.write({"ref": "7"})
        self.env["res.partner"].create(
            {"name": "Other Partner", "ref": "7"}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_ref_duplicate")

    def test_orgnr_hit_adopts_ref_and_touches_nothing(self):
        self.partner.write({"vat": "SE5561234567"})
        self.fake_client._get_results.append(
            {"Customers": [self.fake_customer(CustomerNumber="42")]}
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "42")
        self.assertEqual(self.partner.read(["ref"])[0]["ref"], "42")
        self.assertEqual(len(self.fake_client.calls), 1)
        self.assertEqual(self.fake_client.calls[0][0], "GET")
        self.assertEqual(
            self.fake_client.calls[0][2],
            {"organisationnumber": "5561234567"},
        )

    def test_orgnr_miss_creates_customer_once(self):
        self.partner.write({"vat": "SE5561234567"})
        self.fake_client._get_results.append({"Customers": []})
        self.fake_client._post_results.append(
            {"Customer": {"CustomerNumber": "43"}}
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "43")
        self.assertEqual(self.partner.read(["ref"])[0]["ref"], "43")
        self.assertEqual(len(self.fake_client.calls), 2)
        method, path, payload = self.fake_client.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "customers")
        body = payload["Customer"]
        self.assertEqual(body["Name"], "Customer Step Partner")
        self.assertEqual(body["VATType"], "SEVAT")
        self.assertEqual(body["TermsOfPayment"], "15")
        self.assertEqual(
            body["OrganisationNumber"], "SE5561234567"
        )

    def test_payload_hook_override_is_respected(self):
        self.partner.write({"vat": "SE5561234567"})
        self.fake_client._get_results.append({"Customers": []})
        self.fake_client._post_results.append(
            {"Customer": {"CustomerNumber": "44"}}
        )
        with patch.object(
            type(self.order),
            "_arc_fortnox_customer_payload",
            lambda self: {"Name": "Custom Payload"},
        ):
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(
            self.fake_client.calls[1][2], {"Customer": {"Name": "Custom Payload"}}
        )

    def test_currency_mismatch_stops(self):
        self.partner.write({"ref": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(Currency="NOK")}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(
            context.exception.error_code, "customer_currency_mismatch"
        )

    def test_terms_mismatch_stops(self):
        self.partner.write({"ref": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(TermsOfPayment="30")}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(
            context.exception.error_code, "customer_payment_term_mismatch"
        )

    def test_vat_type_mismatch_stops(self):
        self.partner.write({"ref": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(VATType="EXPORT")}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(
            context.exception.error_code, "customer_vat_type_mismatch"
        )

    def test_no_partner_data_creates_customer_with_minimum(self):
        self.fake_client._get_results.append({"Customers": []})
        self.fake_client._post_results.append(
            {"Customer": {"CustomerNumber": "45"}}
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "45")
        self.assertFalse(
            any(call[0] == "GET" for call in self.fake_client.calls),
            "Without a reference and an organisation number there is "
            "nothing to search for.",
        )
