from odoo import fields, models


class AccountPaymentTerm(models.Model):
    _inherit = "account.payment.term"

    arc_fortnox_code = fields.Char(
        string="Fortnox Payment Term Code",
        copy=False,
        help="The Fortnox terms of payment code this payment term maps to, "
        "for example 15 or 30. An order with an unmapped payment term is "
        "not pushed to Fortnox.",
    )


class UomUom(models.Model):
    _inherit = "uom.uom"

    arc_fortnox_unit = fields.Char(
        string="Fortnox Unit Code",
        copy=False,
        help="The Fortnox unit code this unit of measure maps to, for "
        "example ST or TIM. An order line with an unmapped unit is not "
        "pushed to Fortnox.",
    )


class ProductTemplate(models.Model):
    _inherit = "product.template"

    arc_fortnox_article = fields.Char(
        string="Fortnox Article Number",
        copy=False,
        help="Used on invoice rows when sending article numbers is enabled "
        "in the Fortnox settings. The article must exist in Fortnox.",
    )
    arc_fortnox_account = fields.Char(
        string="Fortnox Revenue Account",
        copy=False,
        help="Fortnox account number for invoice rows of this product. "
        "Falls back to the default revenue account of the VAT mapping.",
    )
