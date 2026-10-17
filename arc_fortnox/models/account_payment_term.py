from odoo import api, fields, models


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


class ProductProduct(models.Model):
    _inherit = "product.product"

    # Per variant, like the internal reference: a Fortnox article is one
    # sellable item, and the variants of a template are different items.
    arc_fortnox_article = fields.Char(
        string="Fortnox Article Number",
        copy=False,
        index="btree_not_null",
        help="Used on invoice rows when sending article numbers is enabled "
        "in the Fortnox settings. The article must exist in Fortnox.",
    )


class ProductTemplate(models.Model):
    _inherit = "product.template"

    arc_fortnox_article = fields.Char(
        string="Fortnox Article Number",
        compute="_compute_arc_fortnox_article",
        inverse="_inverse_arc_fortnox_article",
        help="The Fortnox article number of the single variant. Templates "
        "with several variants carry it per variant.",
    )
    arc_fortnox_account = fields.Char(
        string="Fortnox Revenue Account",
        copy=False,
        help="Fortnox account number for invoice rows of this product. "
        "Falls back to the default revenue account of the VAT mapping.",
    )

    @api.depends("product_variant_ids.arc_fortnox_article")
    def _compute_arc_fortnox_article(self):
        for template in self:
            variants = template.product_variant_ids
            template.arc_fortnox_article = (
                variants.arc_fortnox_article if len(variants) == 1 else False
            )

    def _inverse_arc_fortnox_article(self):
        for template in self:
            if len(template.product_variant_ids) == 1:
                template.product_variant_ids.arc_fortnox_article = template.arc_fortnox_article
