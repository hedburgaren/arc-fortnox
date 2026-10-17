from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    arc_fortnox_customer_number = fields.Char(
        string="Fortnox Customer Number",
        copy=False,
        index="btree_not_null",
        help="The customer number of this company in Fortnox. Set by the "
        "Fortnox bridge when it finds or creates the customer; an existing "
        "Fortnox customer can be linked by entering its number here.",
    )

    def init(self):
        # One Fortnox customer per Odoo partner: two partners on the same
        # number would put each other's invoices on one customer.
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS res_partner_arc_fortnox_customer_number_uniq
            ON res_partner (arc_fortnox_customer_number)
            WHERE arc_fortnox_customer_number IS NOT NULL
            """
        )
