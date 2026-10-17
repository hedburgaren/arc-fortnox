from unittest.mock import patch

from odoo.addons.arc_fortnox.models.fortnox_client import FortnoxError
from odoo.exceptions import UserError

from .common import ArcFortnoxF1Common
from .test_queue_processing import FakeClient


class TestInvoiceDraft(ArcFortnoxF1Common):
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
        self.vat25 = self.env["account.tax"].create(
            {
                "name": "VAT 25%",
                "amount": 25.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
            }
        )
        self.partner = self.env["res.partner"].create(
            {"name": "Draft Builder Partner", "arc_fortnox_customer_number": "7"}
        )
        self.product = self.env["product.product"].create(
            {"name": "Draft Builder Product", "list_price": 100.0}
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
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": 1.0,
                            "price_unit": 100.0,
                            "discount": 0.0,
                            "tax_id": [(6, 0, [self.vat25.id])],
                        },
                    )
                ],
            }
        )
        self.order.action_confirm()
        self.order.order_line[0].product_uom.write({"arc_fortnox_unit": "ST"})
        self.invoicer = self._create_invoicer_user()
        self.fake_client._get_results.append(
            {
                "Customer": {
                    "CustomerNumber": "7",
                    "VATType": "SEVAT",
                    "Currency": self.order.currency_id.name,
                    "TermsOfPayment": "15",
                }
            }
        )

    def _invoice_post_result(self, number="12"):
        self.fake_client._post_results.append(
            {"Invoice": {"DocumentNumber": number}}
        )

    def _post_payload(self):
        for call in self.fake_client.calls:
            if call[0] == "POST":
                return call[2]["Invoice"]
        return None

    def test_draft_leaves_terms_to_fortnox_when_customer_terms_win(self):
        self.company.arc_fortnox_terms_source = "customer"
        self._invoice_post_result("13")
        self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertNotIn("TermsOfPayment", self._post_payload())

    def test_push_posts_draft_and_returns_number(self):
        self._invoice_post_result("12")
        number = self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(number, "12")
        payload = self._post_payload()
        self.assertEqual(payload["CustomerNumber"], "7")
        self.assertNotIn(
            "YourOrderNumber", payload, "Without a customer reference it stays empty."
        )
        self.assertEqual(
            payload["ExternalInvoiceReference1"], self.order.name
        )
        self.assertEqual(payload["Currency"], self.order.currency_id.name)
        self.assertEqual(payload["TermsOfPayment"], "15")
        self.assertFalse(
            {"Booked", "Sent", "ExternalPrint"} & set(payload.keys()),
            "The payload must be a draft only.",
        )

    def test_customer_reference_travels_as_your_order_number(self):
        self.order.client_order_ref = "PO-4711"
        self._invoice_post_result("12")
        self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(self._post_payload()["YourOrderNumber"], "PO-4711")

    def test_row_without_article_by_default(self):
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        row = self._post_payload()["InvoiceRows"][0]
        self.assertNotIn("ArticleNumber", row)
        self.assertEqual(row["DeliveredQuantity"], 1.0)
        self.assertEqual(row["Price"], 100.0)
        self.assertEqual(row["AccountNumber"], "3011")
        self.assertEqual(row["VAT"], 25.0)

    def test_article_number_when_setting_enabled(self):
        self.company.write({"arc_fortnox_send_article_numbers": True})
        self.product.write({"arc_fortnox_article": "A-1"})
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        row = self._post_payload()["InvoiceRows"][0]
        self.assertEqual(row["ArticleNumber"], "A-1")

    def test_product_account_beats_mapping_default(self):
        self.product.write({"arc_fortnox_account": "3050"})
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        row = self._post_payload()["InvoiceRows"][0]
        self.assertEqual(row["AccountNumber"], "3050")

    def test_decimal_quantity_priced_from_full_sheet(self):
        self.order.order_line[0].write(
            {"product_uom_qty": 0.3, "price_unit": 1000.0}
        )
        self.order.order_line[0].product_uom.write({"arc_fortnox_unit": "ST"})
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        row = self._post_payload()["InvoiceRows"][0]
        self.assertEqual(row["DeliveredQuantity"], 0.3)
        self.assertEqual(row["Price"], 1000.0)
        total = row["Price"] * row["DeliveredQuantity"]
        self.assertAlmostEqual(total, self.order.amount_untaxed, places=2)

    def test_discount_travels_as_percentage(self):
        self.order.order_line[0].write({"discount": 10.0})
        self.order.order_line[0].product_uom.write({"arc_fortnox_unit": "ST"})
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        row = self._post_payload()["InvoiceRows"][0]
        self.assertEqual(row["Discount"], 10.0)
        total = row["Price"] * row["DeliveredQuantity"] * 0.9
        self.assertAlmostEqual(total, self.order.amount_untaxed, places=2)

    def test_section_and_note_rows_become_text_rows(self):
        self.order.write(
            {
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "display_type": "line_section",
                            "name": "Section Header",
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "display_type": "line_note",
                            "name": "A note row",
                        },
                    ),
                ]
            }
        )
        self.order.order_line.filtered(
            lambda line: not line.display_type
        ).product_uom.write({"arc_fortnox_unit": "ST"})
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        rows = self._post_payload()["InvoiceRows"]
        self.assertEqual(rows[-2]["Description"], "Section Header")
        self.assertEqual(rows[-2]["Price"], 0)
        self.assertEqual(rows[-2]["DeliveredQuantity"], 0)
        self.assertEqual(rows[-1]["Description"], "A note row")

    def test_long_description_continues_on_text_rows(self):
        self.company.write({"arc_fortnox_max_description_length": 50})
        long_name = (
            "Rundstang transparent med mycket lang beskrivning som ska "
            "fortsatta pa flera rader i Fortnox"
        )
        self.order.order_line[0].write({"name": long_name})
        self.order.order_line[0].product_uom.write({"arc_fortnox_unit": "ST"})
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        rows = self._post_payload()["InvoiceRows"]
        self.assertEqual(rows[0]["Description"], long_name[:50])
        self.assertEqual(rows[1]["Description"], long_name[50:])
        self.assertEqual(rows[1]["Price"], 0)
        self.assertEqual(rows[1]["DeliveredQuantity"], 0)
        self.assertTrue(rows[1]["Description"])

    def test_multi_tax_row_stops_before_post(self):
        vat12 = self.env["account.tax"].create(
            {
                "name": "VAT 12%",
                "amount": 12.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
            }
        )
        self.order.order_line[0].write(
            {"tax_id": [(6, 0, [self.vat25.id, vat12.id])]}
        )
        self.order.order_line[0].product_uom.write({"arc_fortnox_unit": "ST"})
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(context.exception.error_code, "line_multiple_taxes")
        self.assertFalse(
            any(call[0] == "POST" for call in self.fake_client.calls),
            "No POST may happen.",
        )

    def test_account_missing_stops_before_post(self):
        self.mapping.write({"revenue_account": False})
        self.order.order_line[0].product_uom.write({"arc_fortnox_unit": "ST"})
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(context.exception.error_code, "account_missing")
        self.assertFalse(
            any(call[0] == "POST" for call in self.fake_client.calls),
            "No POST may happen.",
        )

    def test_unmapped_unit_stops_before_post(self):
        self.order.order_line[0].product_uom.write({"arc_fortnox_unit": False})
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(context.exception.error_code, "unit_unmapped")
        # The customer step already ran (one GET), but no invoice POST.
        self.assertFalse(
            any(call[0] == "POST" for call in self.fake_client.calls)
        )

    def test_disconnect_allows_missing_invoice(self):
        self.order.sudo().write({"arc_fortnox_invoice_number": "9"})
        self.fake_client._get_results.clear()
        self.fake_client._get_results.append(
            FortnoxError("not found", http_status=404)
        )
        disconnected = []
        with patch.object(
            type(self.order),
            "_arc_fortnox_invoice_disconnected",
            lambda self: disconnected.append(self.id),
        ):
            self.order.with_user(self.invoicer).action_arc_fortnox_disconnect()
        self.assertFalse(self.order.arc_fortnox_invoice_number)
        self.assertEqual(disconnected, [self.order.id])

    def test_disconnect_allows_cancelled_invoice(self):
        self.order.sudo().write({"arc_fortnox_invoice_number": "9"})
        self.fake_client._get_results.clear()
        self.fake_client._get_results.append(
            {"Invoice": {"DocumentNumber": "9", "Cancelled": True}}
        )
        self.order.with_user(self.invoicer).action_arc_fortnox_disconnect()
        self.assertFalse(self.order.arc_fortnox_invoice_number)

    def test_disconnect_refuses_live_invoice(self):
        self.order.sudo().write({"arc_fortnox_invoice_number": "9"})
        self.fake_client._get_results.clear()
        self.fake_client._get_results.append(
            {"Invoice": {"DocumentNumber": "9", "Cancelled": False}}
        )
        with self.assertRaises(UserError):
            self.order.with_user(self.invoicer).action_arc_fortnox_disconnect()
        self.assertEqual(self.order.arc_fortnox_invoice_number, "9")

    def test_disconnect_requires_a_number(self):
        with self.assertRaises(UserError):
            self.order.with_user(self.invoicer).action_arc_fortnox_disconnect()

    def test_excluded_tax_sets_vat_included_false(self):
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertIs(self._post_payload()["VATIncluded"], False)

    def test_included_tax_sets_vat_included(self):
        # Audit P6: a price including VAT must not be sent as a net price.
        vat_incl = self.env["account.tax"].create(
            {
                "name": "VAT 25% incl",
                "amount": 25.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "price_include_override": "tax_included",
            }
        )
        self.order.order_line[0].write(
            {"tax_id": [(6, 0, [vat_incl.id])], "price_unit": 125.0}
        )
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        payload = self._post_payload()
        self.assertIs(payload["VATIncluded"], True)
        self.assertEqual(payload["InvoiceRows"][0]["Price"], 125.0)
        self.assertEqual(payload["InvoiceRows"][0]["VAT"], 25.0)

    def test_mixed_included_and_excluded_taxes_stop(self):
        vat_incl = self.env["account.tax"].create(
            {
                "name": "VAT 12% incl",
                "amount": 12.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "price_include_override": "tax_included",
            }
        )
        self.order.write(
            {
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": 1.0,
                            "price_unit": 10.0,
                            "tax_id": [(6, 0, [vat_incl.id])],
                        },
                    )
                ]
            }
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(context.exception.error_code, "vat_included_mixed")
        self.assertFalse([c for c in self.fake_client.calls if c[0] == "POST"])

    def test_untaxed_line_under_swedish_vat_stops(self):
        # Audit P5: no tax must never mean "let Fortnox pick a rate".
        self.order.order_line[0].tax_id = [(5, 0, 0)]
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(context.exception.error_code, "line_tax_missing")
        self.assertFalse([c for c in self.fake_client.calls if c[0] == "POST"])

    def test_untaxed_line_outside_swedish_vat_sends_zero(self):
        self.mapping.vat_type = "EXPORT"
        self.fake_client._get_results[0]["Customer"]["VATType"] = "EXPORT"
        self.order.order_line[0].tax_id = [(5, 0, 0)]
        self._invoice_post_result()
        self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(self._post_payload()["InvoiceRows"][0]["VAT"], 0)

    def test_fixed_amount_tax_stops(self):
        fixed = self.env["account.tax"].create(
            {
                "name": "Fee",
                "amount": 5.0,
                "amount_type": "fixed",
                "type_tax_use": "sale",
            }
        )
        self.order.order_line[0].tax_id = [(6, 0, [fixed.id])]
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_push_draft(self.fake_client)
        self.assertEqual(context.exception.error_code, "line_tax_not_percent")

    def test_created_customer_survives_a_failed_draft_step(self):
        # Audit D-10: the customer exists in Fortnox now; a retry must find
        # its number on the partner instead of creating it again.
        self.partner.write(
            {"arc_fortnox_customer_number": False, "company_registry": "5561234567"}
        )
        self.fake_client._get_results.clear()
        self.fake_client._post_results.extend(
            [
                {"Customer": {"CustomerNumber": "43"}},
                FortnoxError("timeout", code="timeout"),
            ]
        )
        # try/except on purpose: assertRaises rolls back a savepoint and
        # would hide what the draft step leaves behind.
        try:
            self.order._arc_fortnox_push_draft(self.fake_client)
        except FortnoxError:
            pass
        else:
            self.fail("The draft step should have failed.")
        self.assertEqual(self.partner.arc_fortnox_customer_number, "43")
