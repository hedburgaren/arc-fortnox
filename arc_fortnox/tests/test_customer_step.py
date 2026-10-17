from unittest.mock import patch

from odoo.addons.arc_fortnox.models.fortnox_client import FortnoxError
from odoo.addons.arc_fortnox.models.sale_order import (
    SaleOrder as SaleOrderModel,
)

from odoo.tools import mute_logger

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
        self.assertEqual(normalise("556123 4567"), "5561234567")
        # Audit P4: the real Swedish VAT form, with the trailing 01.
        self.assertEqual(normalise("SE556123456701"), "5561234567")
        self.assertEqual(normalise("se 5561234567 01"), "5561234567")
        # Personal identity numbers with the century.
        self.assertEqual(normalise("19800101-1234"), "8001011234")
        self.assertEqual(normalise("16556123-4567"), "5561234567")
        self.assertEqual(normalise("DE123456789"), "DE123456789")
        self.assertEqual(normalise(False), "")

    def test_org_number_from_registry_then_swedish_vat(self):
        self.partner.write({"vat": "SE556123456701"})
        self.assertEqual(
            self.order._arc_fortnox_partner_org_number(self.partner), "5561234567"
        )
        self.partner.write({"company_registry": "556999-0001"})
        self.assertEqual(
            self.order._arc_fortnox_partner_org_number(self.partner), "5569990001"
        )
        self.partner.write({"company_registry": False, "vat": "DE123456789"})
        self.assertEqual(self.order._arc_fortnox_partner_org_number(self.partner), "")

    def test_existing_ref_returns_number_without_touching_fortnox(self):
        self.partner.write({"arc_fortnox_customer_number": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer()}
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "7")
        self.assertEqual(self.fake_client.calls[0][0], "GET")
        self.assertEqual(self.fake_client.calls[0][1], "customers/7")

    def test_stale_ref_stops_without_creating(self):
        self.partner.write({"arc_fortnox_customer_number": "8"})
        self.fake_client._get_results.append(
            FortnoxError(
                "Customer not found", code="2000410", http_status=404
            )
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_ref_stale")
        self.assertNotIn("POST", [call[0] for call in self.fake_client.calls])

    def test_customer_number_is_unique_across_partners(self):
        self.partner.write({"arc_fortnox_customer_number": "7"})
        other = self.env["res.partner"].create({"name": "Other Partner"})
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"):
            other.write({"arc_fortnox_customer_number": "7"})
            other.flush_recordset()

    def test_customer_number_of_another_legal_person_stops(self):
        # Audit D-8: the number points at somebody else in Fortnox.
        self.partner.write(
            {"arc_fortnox_customer_number": "7", "company_registry": "5561234567"}
        )
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(OrganisationNumber="559999-0000")}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_identity_mismatch")

    def test_customer_number_is_quoted_in_the_path(self):
        self.partner.write({"arc_fortnox_customer_number": "7/../invoices"})
        self.fake_client._get_results.append({"Customer": self.fake_customer()})
        self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(self.fake_client.calls[0][1], "customers/7%2F..%2Finvoices")

    def test_orgnr_hit_adopts_ref_and_touches_nothing(self):
        self.partner.write({"vat": "SE556123456701"})
        self.fake_client._get_results.append(
            {
                "Customers": [
                    self.fake_customer(
                        CustomerNumber="42", OrganisationNumber="5561234567"
                    )
                ]
            }
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "42")
        self.assertEqual(self.partner.read(["arc_fortnox_customer_number"])[0]["arc_fortnox_customer_number"], "42")
        # Both forms are searched, nothing else is called.
        self.assertEqual(
            [(call[0], call[2]) for call in self.fake_client.calls],
            [
                ("GET", {"organisationnumber": "5561234567"}),
                ("GET", {"organisationnumber": "556123-4567"}),
            ],
        )

    def test_orgnr_found_in_dashed_form(self):
        self.partner.write({"company_registry": "5561234567"})
        self.fake_client._get_results.extend(
            [
                {"Customers": []},
                {
                    "Customers": [
                        self.fake_customer(
                            CustomerNumber="42", OrganisationNumber="556123-4567"
                        )
                    ]
                },
            ]
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "42")
        self.assertEqual(
            self.fake_client.calls[1][2], {"organisationnumber": "556123-4567"}
        )

    def test_two_customers_with_the_orgnr_stop(self):
        # Audit D-7.
        self.partner.write({"company_registry": "5561234567"})
        self.fake_client._get_results.append(
            {
                "Customers": [
                    self.fake_customer(CustomerNumber="42", OrganisationNumber="5561234567"),
                    self.fake_customer(CustomerNumber="43", OrganisationNumber="556123-4567"),
                ]
            }
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_orgnr_ambiguous")
        self.assertNotIn("POST", [call[0] for call in self.fake_client.calls])

    def test_inactive_hit_is_skipped_for_the_active_one(self):
        self.partner.write({"company_registry": "5561234567"})
        self.fake_client._get_results.extend(
            [
                {
                    "Customers": [
                        self.fake_customer(CustomerNumber="41", OrganisationNumber="5561234567", Active=False),
                        self.fake_customer(CustomerNumber="42", OrganisationNumber="5561234567", Active=True),
                    ]
                },
                {"Customers": []},
            ]
        )
        self.assertEqual(self.order._arc_fortnox_resolve_customer(self.fake_client), "42")

    def test_only_inactive_hits_stop_without_creating(self):
        self.partner.write({"company_registry": "5561234567"})
        self.fake_client._get_results.extend(
            [
                {
                    "Customers": [
                        self.fake_customer(CustomerNumber="41", OrganisationNumber="5561234567", Active=False)
                    ]
                },
                {"Customers": []},
            ]
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_inactive")
        self.assertNotIn("POST", [call[0] for call in self.fake_client.calls])

    def test_linked_inactive_customer_stops(self):
        self.partner.write({"arc_fortnox_customer_number": "7"})
        self.fake_client._get_results.append({"Customer": self.fake_customer(Active=False)})
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_inactive")

    def test_orgnr_in_both_forms_stops(self):
        # Verifier D-7: one customer stored plain, another with the dash.
        self.partner.write({"company_registry": "5561234567"})
        self.fake_client._get_results.extend(
            [
                {
                    "Customers": [
                        self.fake_customer(CustomerNumber="42", OrganisationNumber="5561234567")
                    ]
                },
                {
                    "Customers": [
                        self.fake_customer(CustomerNumber="43", OrganisationNumber="556123-4567")
                    ]
                },
            ]
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_orgnr_ambiguous")
        self.assertNotIn("POST", [call[0] for call in self.fake_client.calls])

    def test_search_hit_with_other_orgnr_is_not_adopted(self):
        self.partner.write({"company_registry": "5561234567"})
        self.fake_client._get_results.extend(
            [
                {"Customers": [self.fake_customer(CustomerNumber="42", OrganisationNumber="55612345678")]},
                {"Customers": []},
            ]
        )
        self.fake_client._post_results.append({"Customer": {"CustomerNumber": "44"}})
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "44")

    def test_orgnr_miss_creates_customer_once(self):
        self.partner.write({"vat": "SE5561234567"})
        self.fake_client._get_results.append({"Customers": []})
        self.fake_client._post_results.append(
            {"Customer": {"CustomerNumber": "43"}}
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "43")
        self.assertEqual(self.partner.read(["arc_fortnox_customer_number"])[0]["arc_fortnox_customer_number"], "43")
        # The search in both forms of the number, then one create.
        self.assertEqual(len(self.fake_client.calls), 3)
        method, path, payload = self.fake_client.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "customers")
        body = payload["Customer"]
        self.assertEqual(body["Name"], "Customer Step Partner")
        self.assertEqual(body["VATType"], "SEVAT")
        self.assertEqual(body["TermsOfPayment"], "15")
        self.assertEqual(body["OrganisationNumber"], "5561234567")
        self.assertEqual(body["VATNumber"], "SE5561234567")

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
            self.fake_client.calls[-1][2], {"Customer": {"Name": "Custom Payload"}}
        )

    def test_currency_mismatch_stops(self):
        self.partner.write({"arc_fortnox_customer_number": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(Currency="NOK")}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(
            context.exception.error_code, "customer_currency_mismatch"
        )

    def test_terms_mismatch_stops(self):
        self.partner.write({"arc_fortnox_customer_number": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(TermsOfPayment="30")}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(
            context.exception.error_code, "customer_payment_term_mismatch"
        )

    def test_terms_mismatch_ignored_when_fortnox_terms_win(self):
        self.company.arc_fortnox_terms_source = "customer"
        self.partner.write({"arc_fortnox_customer_number": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(TermsOfPayment="30")}
        )
        number = self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(number, "7")

    def test_vat_type_mismatch_stops(self):
        self.partner.write({"arc_fortnox_customer_number": "7"})
        self.fake_client._get_results.append(
            {"Customer": self.fake_customer(VATType="EXPORT")}
        )
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(
            context.exception.error_code, "customer_vat_type_mismatch"
        )

    def test_partner_without_number_or_orgnr_stops(self):
        # Neither a Fortnox number nor an organisation number: a create
        # could duplicate a customer already in Fortnox, so nothing is sent.
        with self.assertRaises(FortnoxError) as context:
            self.order._arc_fortnox_resolve_customer(self.fake_client)
        self.assertEqual(context.exception.error_code, "customer_unlinked")
        self.assertEqual(self.fake_client.calls, [])
        self.assertFalse(self.partner.arc_fortnox_customer_number)

    def test_payload_uses_invoice_address_and_type(self):
        # Audit D-9.
        self.partner.write(
            {
                "is_company": True,
                "vat": "SE556123456701",
                "street": "Head Office 1",
                "email": "office@example.com",
            }
        )
        invoice_address = self.env["res.partner"].create(
            {
                "name": "Accounts",
                "type": "invoice",
                "parent_id": self.partner.id,
                "street": "Invoice Street 2",
                "zip": "58222",
                "city": "Linköping",
                "email": "invoices@example.com",
            }
        )
        self.order.partner_invoice_id = invoice_address
        body = self.order._arc_fortnox_customer_payload()
        self.assertEqual(body["Type"], "COMPANY")
        self.assertEqual(body["OrganisationNumber"], "5561234567")
        self.assertEqual(body["VATNumber"], "SE556123456701")
        self.assertEqual(body["Address1"], "Invoice Street 2")
        self.assertEqual(body["City"], "Linköping")
        self.assertEqual(body["EmailInvoice"], "invoices@example.com")

    def test_private_person_payload(self):
        self.partner.write({"is_company": False, "company_registry": "19800101-1234"})
        body = self.order._arc_fortnox_customer_payload()
        self.assertEqual(body["Type"], "PRIVATE")
        self.assertEqual(body["OrganisationNumber"], "19800101-1234")
        self.assertNotIn("DefaultTemplates", body, "No template configured, none sent.")

    def test_new_customer_gets_the_template_of_its_type(self):
        self.company.write(
            {
                "arc_fortnox_invoice_template_private": "QR",
                "arc_fortnox_invoice_template_company": "STD",
            }
        )
        self.partner.write({"is_company": False})
        self.assertEqual(
            self.order._arc_fortnox_customer_payload()["DefaultTemplates"], {"Invoice": "QR"}
        )
        self.partner.write({"is_company": True})
        self.assertEqual(
            self.order._arc_fortnox_customer_payload()["DefaultTemplates"], {"Invoice": "STD"}
        )

    def test_customer_type_hook_decides_the_type(self):
        with patch.object(
            type(self.order), "_arc_fortnox_customer_type", lambda self, partner: "PRIVATE"
        ):
            self.assertEqual(self.order._arc_fortnox_customer_payload()["Type"], "PRIVATE")
