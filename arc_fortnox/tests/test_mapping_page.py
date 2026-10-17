import base64
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError

from .common import ArcFortnoxF1Common

CUSTOMER_EXPORT = (
    "customer_number,name,organisation_number,active,type,invoice_city\n"
    "1001,Plasttillverkarna Linköping AB,5561234567,1,company,LINKÖPING\n"
    "1002,Verkstad Jonsson AB,,1,company,MOTALA\n"
    "1003,Okänd Firma AB,5569999999,1,company,UMEÅ\n"
    "1004,Gammal Kund AB,5568888888,0,company,LUND\n"
)
ARTICLE_EXPORT = (
    "Artikelnummer;Benämning;Aktiv\n"
    "A1000001;Platta i PE-HD 10 mm;Ja\n"
    "A1000002;Rundstång i POM 20 mm;Ja\n"
)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.pages[params["page"] - 1]


class TestMappingPage(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.invoicer = self._create_invoicer_user()
        self.admin = self.env.ref("base.user_admin")
        self.records = self.env["arc.fortnox.record"]
        Partner = self.env["res.partner"]
        self.plast = Partner.create(
            {"name": "Plasttillverkarna i Linköping AB", "is_company": True, "company_registry": "556123-4567"}
        )
        self.jonsson = Partner.create({"name": "Verkstads Jonsson AB", "is_company": True})
        self.product = self.env["product.product"].create(
            {"name": "Platta i PE-HD 10 mm", "default_code": "A1000001"}
        )

    def _import(self, text, record_type="customer"):
        wizard = self.env["arc.fortnox.record.import"].with_user(self.admin).create(
            {"record_type": record_type, "file": base64.b64encode(text.encode("utf-8"))}
        )
        wizard.action_import()
        return {
            record.fortnox_number: record
            for record in self.records.search([("record_type", "=", record_type)])
        }

    def test_import_suggests_by_organisation_number_then_name(self):
        rows = self._import(CUSTOMER_EXPORT)
        self.assertEqual(rows["1001"].suggested_partner_id, self.plast)
        self.assertEqual(rows["1001"].suggestion_reason, "organisation number")
        self.assertEqual(rows["1001"].state, "suggested")
        self.assertEqual(rows["1002"].suggested_partner_id, self.jonsson)
        self.assertEqual(rows["1002"].suggestion_reason, "similar name")
        self.assertEqual(rows["1003"].state, "open")
        self.assertFalse(rows["1004"].fortnox_active)
        self.assertEqual(rows["1001"].city, "LINKÖPING")

    def test_use_suggestion_writes_the_number_on_the_partner_only(self):
        rows = self._import(CUSTOMER_EXPORT)
        rows["1001"].with_user(self.invoicer).action_use_suggestion()
        self.assertEqual(self.plast.arc_fortnox_customer_number, "1001")
        self.assertEqual(rows["1001"].state, "linked")

    def test_search_and_pick_another_partner(self):
        rows = self._import(CUSTOMER_EXPORT)
        rows["1003"].with_user(self.invoicer).write({"partner_id": self.jonsson.id})
        self.assertEqual(self.jonsson.arc_fortnox_customer_number, "1003")

    def test_partner_with_another_number_is_refused(self):
        rows = self._import(CUSTOMER_EXPORT)
        rows["1001"].with_user(self.invoicer).action_use_suggestion()
        with self.assertRaises(UserError):
            rows["1003"].with_user(self.invoicer).write({"partner_id": self.plast.id})

    def test_unlink_and_relink_moves_the_number(self):
        rows = self._import(CUSTOMER_EXPORT)
        record = rows["1002"].with_user(self.invoicer)
        record.write({"partner_id": self.plast.id})
        record.write({"partner_id": self.jonsson.id})
        self.assertFalse(self.plast.arc_fortnox_customer_number)
        self.assertEqual(self.jonsson.arc_fortnox_customer_number, "1002")
        record.action_unlink_odoo()
        self.assertFalse(self.jonsson.arc_fortnox_customer_number)

    def test_number_already_in_odoo_is_linked_on_import(self):
        self.jonsson.arc_fortnox_customer_number = "1003"
        rows = self._import(CUSTOMER_EXPORT)
        self.assertEqual(rows["1003"].partner_id, self.jonsson)
        self.assertEqual(rows["1003"].state, "linked")

    def test_reimport_refreshes_fortnox_side_and_keeps_the_link(self):
        rows = self._import(CUSTOMER_EXPORT)
        rows["1001"].with_user(self.invoicer).action_use_suggestion()
        rows = self._import(CUSTOMER_EXPORT.replace("LINKÖPING", "NORRKÖPING"))
        self.assertEqual(rows["1001"].city, "NORRKÖPING")
        self.assertEqual(rows["1001"].partner_id, self.plast)
        self.assertEqual(len(rows), 4)

    def test_difference_is_shown_and_odoo_is_never_rewritten(self):
        rows = self._import(CUSTOMER_EXPORT)
        record = rows["1001"].with_user(self.invoicer)
        record.action_use_suggestion()
        self.assertEqual(record.difference, "name")
        self.assertEqual(self.plast.name, "Plasttillverkarna i Linköping AB")

    def test_articles_from_swedish_export_suggest_by_internal_reference(self):
        rows = self._import(ARTICLE_EXPORT, "article")
        self.assertEqual(rows["A1000001"].suggested_product_id, self.product)
        self.assertEqual(rows["A1000002"].state, "open")
        rows["A1000001"].with_user(self.invoicer).action_use_suggestion()
        self.assertEqual(self.product.arc_fortnox_article, "A1000001")
        self.assertEqual(self.product.product_tmpl_id.arc_fortnox_article, "A1000001")

    def test_article_number_is_per_variant(self):
        attribute = self.env["product.attribute"].create(
            {"name": "Tjocklek", "value_ids": [(0, 0, {"name": "10 mm"}), (0, 0, {"name": "20 mm"})]}
        )
        template = self.env["product.template"].create(
            {
                "name": "Platta i POM",
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": attribute.id, "value_ids": [(6, 0, attribute.value_ids.ids)]})
                ],
            }
        )
        first, second = template.product_variant_ids
        first.default_code, second.default_code = "A2000010", "A2000020"
        rows = self._import("Artikelnummer;Benämning\nA2000020;Platta i POM 20 mm\n", "article")
        rows["A2000020"].with_user(self.invoicer).action_use_suggestion()
        self.assertEqual(second.arc_fortnox_article, "A2000020")
        self.assertFalse(first.arc_fortnox_article)
        self.assertFalse(template.arc_fortnox_article)

    def test_plain_user_cannot_map(self):
        rows = self._import(CUSTOMER_EXPORT)
        plain = self._create_plain_user()
        with self.assertRaises(AccessError):
            rows["1001"].with_user(plain).write({"partner_id": self.plast.id})
        self.assertFalse(self.plast.arc_fortnox_customer_number)

    def test_invoicer_cannot_import(self):
        wizard = self.env["arc.fortnox.record.import"].create(
            {"record_type": "customer", "file": base64.b64encode(CUSTOMER_EXPORT.encode())}
        )
        with self.assertRaises(AccessError):
            wizard.with_user(self.invoicer).action_import()

    def test_file_without_number_column_is_refused(self):
        with self.assertRaises(UserError):
            self._import("foo,bar\n1,2\n")

    def test_load_from_fortnox_reads_every_page(self):
        client = FakeClient(
            [
                {
                    "Customers": [{"CustomerNumber": "1001", "Name": "A", "OrganisationNumber": "556123-4567"}],
                    "MetaInformation": {"@TotalPages": 2},
                },
                {
                    "Customers": [{"CustomerNumber": "1002", "Name": "B"}],
                    "MetaInformation": {"@TotalPages": 2},
                },
            ]
        )
        with patch.object(type(self.company), "arc_fortnox_client", lambda company: client):
            self.records.with_user(self.admin).action_arc_fortnox_fetch_customers()
        self.assertEqual([call[1]["page"] for call in client.calls], [1, 2])
        rows = self.records.search([("record_type", "=", "customer")])
        self.assertEqual(sorted(rows.mapped("fortnox_number")), ["1001", "1002"])
        self.assertEqual(rows.filtered(lambda r: r.fortnox_number == "1001").suggested_partner_id, self.plast)

    # Verifier findings on task 136, 2026-10-18.

    def test_private_customer_is_suggested_among_persons_and_can_be_linked(self):
        person = self.env["res.partner"].create({"name": "Anna Andersson", "is_company": False})
        rows = self._import(
            "customer_number,name,organisation_number,active,type,invoice_city\n"
            "2001,Anna Anderson,,1,private,KARLSTAD\n"
        )
        self.assertEqual(rows["2001"].suggested_partner_id, person)
        rows["2001"].with_user(self.invoicer).action_use_suggestion()
        self.assertEqual(person.arc_fortnox_customer_number, "2001")

    def test_company_customer_is_not_suggested_a_person(self):
        self.env["res.partner"].create({"name": "Okänd Firma AB", "is_company": False})
        rows = self._import(CUSTOMER_EXPORT)
        self.assertFalse(rows["1003"].suggested_partner_id)

    def test_fortnox_fields_cannot_be_edited(self):
        rows = self._import(CUSTOMER_EXPORT)
        with self.assertRaises(UserError):
            rows["1001"].with_user(self.invoicer).write({"name": "Ändrat namn"})
        self.assertEqual(rows["1001"].name, "Plasttillverkarna Linköping AB")
