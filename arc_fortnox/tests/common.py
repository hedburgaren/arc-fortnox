from odoo.tests import TransactionCase
from odoo.tests.common import tagged


@tagged("post_install", "-at_install")
class ArcFortnoxF1Common(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.group_invoicer = cls.env.ref("arc_fortnox.group_invoicer")
        cls.group_user = cls.env.ref("base.group_user")

    def _create_user(self, login, group_refs):
        groups = [self.env.ref(ref) for ref in group_refs]
        return self.env["res.users"].create(
            {
                "name": login.replace("_", " ").title(),
                "login": login,
                "email": f"{login}@example.com",
                "groups_id": [(6, 0, [group.id for group in groups])],
            }
        )

    def _create_plain_user(self, login="arcfx_plain"):
        return self._create_user(login, ["base.group_user"])

    def _create_invoicer_user(self, login="arcfx_invoicer"):
        return self._create_user(
            login,
            [
                "base.group_user",
                "sales_team.group_sale_salesman_all_leads",
                "arc_fortnox.group_invoicer",
            ],
        )

    def _create_confirmed_order(self):
        product = self.env["product.product"].create(
            {"name": "Arc Fortnox Test Product", "list_price": 100.0}
        )
        partner = self.env["res.partner"].create(
            {"name": "Arc Fortnox Test Partner"}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "product_uom_qty": 1,
                            "price_unit": 100.0,
                        },
                    )
                ],
            }
        )
        order.action_confirm()
        return order
