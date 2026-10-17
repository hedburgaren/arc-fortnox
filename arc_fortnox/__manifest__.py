{
    'name': 'Fortnox Bridge (Invoice Drafts)',
    'version': '18.0.1.1.0',
    'category': 'Sales/Sales',
    'summary': 'Push confirmed sales orders to Fortnox as invoice drafts.',
    'description': """
Fortnox Bridge: push sales order invoice drafts to Fortnox.

Confirmed sales orders are pushed through a queue to Fortnox, where an
invoice draft is created. Odoo never books, sends or emails anything; a
human reviews and sends the draft from Fortnox. Fortnox owns the
bookkeeping.

Highlights: OAuth2 credentials per company with tokens readable by
administrators only, a queue with retries and backoff driven by a cron,
customers created once and never overwritten, mapping tables for VAT type,
payment terms, units and revenue accounts, and a configurable trigger with
a manual button by default and optional automatic enqueue from a domain on
sale orders.

Every installation registers its own app in the Fortnox developer portal.
Client credentials are per company and are never shipped with the module.
""",
    'author': 'ARC Gruppen',
    'website': 'https://github.com/arcgruppen/arc-fortnox',
    'license': 'LGPL-3',
    'depends': ['sale_management'],
    'data': [
        'security/arc_fortnox_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/sale_order_views.xml',
        'views/arc_fortnox_views.xml',
        'views/inherit_views.xml',
    ],
    'application': False,
    'installable': True,
}
