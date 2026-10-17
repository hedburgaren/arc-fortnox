# arc_fortnox

Odoo 18 Community module that pushes confirmed sales orders to
[Fortnox](https://www.fortnox.se) as invoice **drafts**. Odoo never books,
sends, prints or emails anything: a human reviews the draft in Fortnox and
sends it from there. Fortnox owns the bookkeeping.

Licensed LGPL-3. See `LICENSE` (LGPL-3.0) and `COPYING` (GPL-3.0,
referenced by the LGPL).

## How it works

1. A button on a confirmed sales order (or an optional automatic trigger)
   puts the order in a queue. No API call happens at that point.
2. A cron processes the queue every 5 minutes: it resolves or creates the
   Fortnox customer, posts the invoice draft and writes the Fortnox invoice
   number back onto the order.
3. Failures retry on a backoff schedule. When the attempts run out, the
   queue entry becomes a hard error with an activity on the order.
4. A lost answer, a crash or a failed write back cannot duplicate drafts:
   every attempt first searches Fortnox for a live draft whose
   `ExternalInvoiceReference1` equals the order name, adopts exactly one,
   and stops when it finds more than one. The number is recorded before
   any follow up runs, so a failing companion hook raises an activity
   instead of a retry.
5. Disconnecting an order is only allowed when the Fortnox invoice is
   missing or cancelled there. Odoo never writes to an existing Fortnox
   document.
6. An order that is cancelled, or already carries a Fortnox number, is
   never sent; cancelling an order withdraws its pending queue entries.

## Requirements

- Odoo 18 Community Edition with `sale_management`
- A Fortnox subscription that allows integrations
- Your own app registration in the Fortnox developer portal. Client
  credentials are per installation and are never shipped with this module.

## Setup

### 1. Register your app at Fortnox

1. Create a developer account and register an application in the Fortnox
   developer portal.
2. Scopes: `invoice` and `customer`. Nothing else is needed.
3. Redirect URI: shown in Odoo under Settings, Fortnox, Client
   Credentials. It is your Odoo base URL plus `/arc_fortnox/callback`.
   Odoo rewrites `web.base.url` when an administrator logs in unless
   `web.base.url.freeze` is set; to pin the URI regardless, set the
   parameter `arc_fortnox.redirect_uri` to the exact value you registered.
4. Copy the client id and client secret into Settings, Fortnox, Client
   Credentials (per company), or keep them out of the database entirely by
   setting the parameters `arc_fortnox.client_id` and
   `arc_fortnox.client_secret` in `ir.config_parameter` or in odoo.conf.
   The settings fields win when both are present.
5. Press **Authorize with Fortnox** in the settings and log in to Fortnox.

### 2. Map your data (Settings and configuration menus)

Nothing works until it is mapped. Unmapped values stop the push with a
clear error instead of guessing:

| What | Where |
|---|---|
| Fiscal position to Fortnox VAT type and a default revenue account; one row without a fiscal position covers orders that carry none | Sales, Configuration, Fortnox, Fortnox VAT Mappings (administrators edit, invoicers read) |
| Payment terms to Fortnox payment term codes | Field `Fortnox Payment Term Code` on each payment term |
| Units of measure to Fortnox unit codes | Field `Fortnox Unit Code` on each unit of measure |
| Optional: article numbers and per product revenue accounts | Fields on the product form |
| Optional: link existing Fortnox customers | Field `Fortnox Customer Number` on the company partner |
| Optional: freight products, whose lines go to the invoice's Freight field instead of rows | Settings, Fortnox, Invoice Rows |

### Map existing Fortnox customers and articles

Sales, Configuration, Fortnox, **Map Fortnox Customers** and **Map
Fortnox Articles** list the Fortnox records on the left, read only, and
the Odoo record on the right. Load them with **Load from Fortnox** or
**Import Fortnox Export** (a CSV export from Fortnox). Each row suggests
an Odoo customer (same organisation number, or a close name) or product
variant (same internal reference); confirm with **Use Suggestion** or pick
another. The link is stored in Odoo only, as the Fortnox customer number
on the partner and the article number on the variant. A linked row whose
name or organisation number differs is flagged; neither side is changed,
since the page is for linking, and data is corrected where it belongs by
hand.

Companion modules can add warnings shown before an order is queued by
overriding `sale.order._arc_fortnox_enqueue_warnings()`. The button then
asks for confirmation; the automatic trigger leaves such orders out and
schedules an activity instead.

VAT types: `SEVAT` (Swedish VAT), `EUREVERSEDVAT` (EU reverse VAT),
`EUVAT`, `EXPORT` (outside the EU).

### 3. Choose the trigger

Under Settings, Fortnox, Invoicing Trigger:

- `Manual only` (default): invoicers press **Queue for Fortnox** on the
  order. The button is visible to the *Fortnox Invoicer* group on
  confirmed orders without a Fortnox invoice number and without an active
  queue entry.
- `Automatic on order domain`: a cron also queues confirmed orders
  matching a domain you write, for example a custom stage field from
  another module. Invalid domains are rejected on save. Switching the mode
  on stamps a start date, and only orders confirmed after it are queued;
  move the date back yourself to include older orders.

### 4. Tune the API behavior (all optional, all have defaults)

Throttle (calls per period), timeouts, retry backoff schedule, the attempt
count that turns a queue entry into a hard error, the description length
that triggers continuation rows, and whether products with article numbers
send `ArticleNumber` instead of free text rows.

Payment terms come from the order by default: the draft carries the
order's term, and a Fortnox customer with other terms stops the push. Set
**Payment Terms From** to *The Fortnox customer's terms* when Fortnox is
where terms are kept: drafts then carry no terms and Fortnox applies the
customer's own, while a customer the module creates gets the order's term.

A company partner with neither a Fortnox customer number nor an
organisation number stops the push before any request, since a new
customer could duplicate one already in Fortnox.

## Who may do what

| Group | Rights |
|---|---|
| Fortnox Invoicer | Queue orders, disconnect drafts, retry or cancel failed queue entries, read the queue and mappings |
| Administration / Settings | Everything, including credentials and tokens |
| Everyone else | Sees the Fortnox invoice number on orders, nothing else |

Tokens are stored behind field level groups and the credential model is
inaccessible over RPC for anyone outside Administration / Settings; the
module reads it with `sudo()` internally.

## What Odoo deliberately does not do

- It never books, sends, prints or emails an invoice. Drafts only.
- It never updates an existing Fortnox customer in v1. A customer is
  matched on the organisation number (the partner's company registry, or
  a Swedish VAT number reduced to it); exactly one match is adopted into
  the partner's Fortnox customer number, more than one stops. A linked
  customer must carry the partner's organisation number, and a mismatch
  in VAT type, currency or payment terms stops the push with a distinct
  error instead of overwriting anything.
- It never syncs the product catalogue. Rows travel as free text rows with
  quantity, price, unit, account and VAT, so no article database is
  needed.
- It never reads payment status back from Fortnox. That is a possible
  later version.
- The Fortnox header fields `Freight` and `AdministrationFee` are unused:
  delivery and other extra lines travel as ordinary order lines with their
  own accounts.

## Extension points

Private companion modules adapt behavior by overriding these `sale.order`
methods (documented in the source):

- `_arc_fortnox_enqueue_allowed()`: extra gate before enqueue.
- `_arc_fortnox_customer_payload()`: change the customer creation payload.
- `_arc_fortnox_line_values(line, mapping)`: change an invoice row.
- `_arc_fortnox_invoice_created()`: called when the draft exists.
- `_arc_fortnox_invoice_disconnected()`: called after a disconnect.
- `_arc_fortnox_push_draft(client)`: replace the whole draft push.

## Testing

The suite runs without network access. With the repository on your addons
path:

```
odoo -d yourdb -i arc_fortnox --test-enable --test-tags /arc_fortnox --stop-after-init
```

## Troubleshooting

| Error code on the queue entry | Meaning |
|---|---|
| `vat_mapping_missing` | The order's fiscal position has no row in Fortnox VAT Mappings |
| `payment_term_unmapped` | The order's payment term has no Fortnox code |
| `unit_unmapped` | An order line's unit has no Fortnox unit code |
| `account_missing` | Neither the product nor the VAT mapping provides an account |
| `line_multiple_taxes` | A row carries more than one tax |
| `customer_ref_stale` | The partner's Fortnox customer number points at a customer that does not exist |
| `customer_identity_mismatch` | The linked Fortnox customer carries another organisation number |
| `customer_orgnr_ambiguous` | More than one Fortnox customer has the partner's organisation number |
| `line_tax_missing` | A row has no tax on an order under Swedish VAT |
| `line_tax_not_percent` | A row's tax is not a plain percentage |
| `vat_included_mixed` | The order mixes taxes included in the price with excluded ones |
| `invoice_reference_ambiguous` | Fortnox holds more than one live invoice with the order's reference |
| `customer_vat_type_mismatch`, `customer_currency_mismatch`, `customer_payment_term_mismatch` | The existing Fortnox customer disagrees with the order: fix one side, nothing was overwritten |
| `unauthorized` / reset connection | The token was rejected and could not be refreshed: authorize again |

Every failed request is logged with method, path, HTTP status and Fortnox
error code. Payloads, customer data and tokens are never logged.

## Copies of the database

The module ships `data/neutralize.sql`. A neutralized copy (staging, a
restored backup) loses the Fortnox tokens, the client secret and the
automatic trigger, so it can neither spend the live refresh token, which
Fortnox invalidates on every refresh, nor write to the live books.
Authorize the copy again against a Fortnox sandbox company. A copy made
without neutralization still carries everything; do not run one.
