# arc_fortnox knowledge document

Purpose: push confirmed sales orders to Fortnox as invoice drafts through
a queue, with retries and recovery that never duplicates a draft. Odoo
never books, sends, prints or emails; Fortnox owns bookkeeping. LGPL-3,
public repository. Version 18.0.1.2.0.

## Key models

- `res.company` (inherit): all settings per company. Client id and secret
  carry `groups="base.group_system"`; resolution order is company field,
  then `arc_fortnox.client_id` / `arc_fortnox.client_secret` in
  `ir.config_parameter`, then odoo.conf. Trigger mode, validated trigger
  domain and `arc_fortnox_trigger_since` (stamped by `write` when the
  mode switches to domain, cleared when it switches back), throttle,
  timeouts, backoff schedule, max attempts, description split length,
  send article numbers. `arc_fortnox_client()` returns the API client;
  `arc_fortnox_authorize_url()` arms the OAuth nonce and returns the URL
  (`response_type=code`, `access_type=offline`, `account_type=service`);
  `_arc_fortnox_redirect_uri()` returns the parameter
  `arc_fortnox.redirect_uri` when set, else web.base.url plus
  `/arc_fortnox/callback`.
- `res.partner` (inherit): `arc_fortnox_customer_number`, unique where
  set (partial index in `init()`), shown on company partners to invoicers.
- `arc.fortnox.credential`: one row per company. ACL: `base.group_system`
  only; token and nonce fields have field level groups. States:
  not_authorized, authorizing, authorized, error. Methods:
  `_arc_fortnox_on_unauthorized(rejected_token)` returns the new access
  token or False; `_arc_fortnox_refresh_tokens(rejected_token)` runs on a
  cursor of its own (`registry.cursor()`), SELECT FOR UPDATE, skips the
  refresh when the row already holds another access token than the
  rejected one, retries a serialization failure, commits, returns the new
  access token; `_arc_fortnox_store_tokens` also clears
  `expiry_warned_at`; `_arc_fortnox_handle_callback` (constant time nonce,
  single use); `_arc_fortnox_cron_warn_expiry` (email plus log from day
  38).
- `arc.fortnox.queue`: one active entry (pending, sending, error) per
  order via partial unique index in `init()`. ACL: invoicers read only,
  `base.group_system` full. `action_retry` (error to pending, attempts
  reset) and `action_cancel` (pending or error) check the invoicer group
  and write with sudo. Cron `_arc_fortnox_cron_process` every 5 minutes,
  one savepoint and one commit per entry, pending committed as sending
  before any API call. A `FortnoxError` is caught inside the savepoint
  (what the steps kept stays); any other error, a database error
  included, rolls the entry back and the run goes on.
  `_arc_fortnox_process_one`: order no longer `sale` or already numbered
  cancels the entry; then `sale.order._arc_fortnox_find_existing_draft`
  in a savepoint, on every attempt; a hit is adopted, otherwise
  `_arc_fortnox_push_draft`. `_arc_fortnox_mark_done` writes number and
  done first, then the hook and chatter in a savepoint; a failure there
  becomes an activity, never a retry. `commit=False` exists for tests.
- `arc.fortnox.vat.mapping`: fiscal position (optional) to Fortnox VAT
  type (SEVAT, EUREVERSEDVAT, EUVAT, EXPORT) and default revenue account.
  At most one row without fiscal position per company (partial index),
  used for orders without one. ACL: invoicers read, admins write.
- `sale.order` (inherit): `arc_fortnox_invoice_number`, queue buttons
  gated on `arc_fortnox.group_invoicer`, hooks, push draft, row building,
  customer resolution, mapping resolvers, `_action_cancel` withdrawing
  pending and error entries. All stop with `FortnoxError` and stable
  codes before any request when something is unmapped.
- `sale.order.line` (inherit): `_arc_fortnox_unit_code`,
  `_arc_fortnox_row_account` (product field beats mapping default).
- Fields on `account.payment.term` (`arc_fortnox_code`), `uom.uom`
  (`arc_fortnox_unit`), `product.template` (`arc_fortnox_article`,
  `arc_fortnox_account`).

## Flows

Enqueue (button or trigger cron) creates a pending queue entry, no API
call. The trigger cron only takes orders with `date_order` from
`arc_fortnox_trigger_since`. The processing cron: set sending and commit;
check the order; search Fortnox invoices by `externalinvoicereference1`
(cancelled ignored, exact reference, list entries without the fields are
read in full, two live hits stop with `invoice_reference_ambiguous`);
adopt or push. Push: customer step in a savepoint, then the draft in a
savepoint, so a created customer keeps its number when the draft fails.

Customer step: partner number set, GET the customer (quoted path), check
the organisation number (`customer_identity_mismatch`) and VAT type,
currency, terms. No number: organisation number from `company_registry`,
else from a Swedish VAT number (`SE` and trailing `01` dropped; the
twelve digit forms with `16`, `19` or `20` lose the prefix); search
`customers?organisationnumber=` with and without dash, always both,
compare after normalisation, exactly one is adopted, more stop
(`customer_orgnr_ambiguous`). Neither a customer number nor an
organisation number stops before any request (`customer_unlinked`): a
create could duplicate a customer already in Fortnox. An organisation
number Fortnox does not know: create from
`_arc_fortnox_customer_payload` (Type COMPANY or PRIVATE, organisation
number and VAT number in their own fields, invoice address and email).

Draft: `ExternalInvoiceReference1` = order name, `YourOrderNumber` =
`client_order_ref` when set, `VATIncluded` from the taxes (mixed stops),
every row with an explicit `VAT` (untaxed under SEVAT stops, otherwise 0;
non percent taxes stop).

Token endpoint: HTTP Basic client authentication, the body carries only
the grant. A 401 refreshes once and retries with the returned token.
Disconnect requires the invoice to be missing or cancelled in Fortnox.

## Security notes

Tokens and the nonce are invisible over RPC outside
`base.group_system`; the client reads them with `sudo()`. Logging carries
method, path, HTTP status and Fortnox error code. Never commit client
secrets; they belong in settings or odoo.conf. `data/neutralize.sql`
drops tokens, nonce, client secret (field and parameter) and sets the
trigger to manual.

## Gotchas

- Odoo 18: `list` not `tree` in view arch; `groups_id` on res.users;
  salesman groups live in `sales_team`; sale.order has no
  `commercial_partner_id` (use `partner_invoice_id.commercial_partner_id`);
  `account.tax.price_include` is computed from `price_include_override`.
- `registry.cursor()` in a `TransactionCase` is a real connection that
  commits for real. The test base class enters registry test mode so the
  refresh cursor runs on top of the test transaction; plain SQL on it
  needs `flush_all()` first.
- Tests: Odoo's `assertRaises` rolls back a savepoint on the expected
  exception, so writes asserted after a raise must use try/except; flush
  deferred INSERTs before partial unique index tests.
- Open for the sandbox run: the rate limit default (25 calls / 5 s), the
  invoice list filter `externalinvoicereference1` and the fields it
  returns, the organisation number format Fortnox stores and searches.
