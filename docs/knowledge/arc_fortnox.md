# arc_fortnox knowledge document

Purpose: push confirmed sales orders to Fortnox as invoice drafts through
a queue, with retries and crash recovery. Odoo never books, sends, prints
or emails; Fortnox owns bookkeeping. LGPL-3, public repository.

## Key models

- `res.company` (inherit): all settings per company. Client id and secret
  carry `groups="base.group_system"`; resolution order is company field,
  then `arc_fortnox.client_id` / `arc_fortnox.client_secret` in
  `ir.config_parameter`, then odoo.conf. Trigger mode and validated
  trigger domain, throttle, timeouts, backoff schedule, max attempts,
  description split length, send article numbers.
  `arc_fortnox_client()` returns the API client;
  `arc_fortnox_authorize_url()` arms the OAuth nonce and returns the URL;
  `_arc_fortnox_redirect_uri()` derives web.base.url plus
  `/arc_fortnox/callback`.
- `arc.fortnox.credential`: one row per company. ACL: `base.group_system`
  only; token and nonce fields have field level groups. States:
  not_authorized, authorizing, authorized, error. Holds the OAuth state
  nonce and its expiry, tokens, `expiry_warned_at` deduplication. Methods:
  `_arc_fortnx_on_unauthorized` (refresh and report success), 
  `_arc_fortnox_refresh_tokens` (SELECT FOR UPDATE, rotation),
  `_arc_fortnox_handle_callback` (constant time nonce, single use),
  `_arc_fortnox_cron_warn_expiry` (email plus log from day 38).
- `arc.fortnox.queue`: one active entry (pending, sending, error) per
  order via partial unique index in `init()`. Cron
  `_arc_fortnox_cron_process` every 5 minutes, one commit per entry,
  pending committed as sending before any API call, `recover` flag for
  entries found in sending (search by YourOrderNumber first, adopt the
  draft). Backoff from settings, hard error plus mail activity on the
  order after max attempts. `commit=False` parameter exists for tests.
- `arc.fortnox.vat.mapping`: fiscal position to Fortnox VAT type
  (SEVAT, EUREVERSEDVAT, EUVAT, EXPORT) and default revenue account.
- `sale.order` (inherit): `arc_fortnox_invoice_number`, queue buttons
  gated on `arc_fortnox.group_invoicer`, hooks, push draft, row building,
  customer resolution, mapping resolvers. All stop with `FortnoxError`
  and stable codes before any request when something is unmapped.
- `sale.order.line` (inherit): `_arc_fortnx_unit_code`,
  `_arc_fortnx_row_account` (product field beats mapping default).
- Fields on `account.payment.term` (`arc_fortnox_code`), `uom.uom`
  (`arc_fortnox_unit`), `product.template` (`arc_fortnox_article`,
  `arc_fortnox_account`).

## Flows

Enqueue (button or trigger cron) creates a pending queue entry, no API
call. The processing cron: set sending and commit, resolve customer
(reference GET, organisation number search adopting into ref, single
create via hook), POST /3/invoices draft only, write back the number, run
`_arc_fortnox_invoice_created`. 401 refreshes the token once and retries.
Disconnect requires the invoice to be missing or cancelled in Fortnox.

## Security notes

Tokens and the nonce are invisible over RPC outside
`base.group_system`; the client reads them with `sudo()`. Logging carries
queue entry id, order name, HTTP status and Fortnox error code only.
Never commit client secrets; they belong in settings or odoo.conf.

## Gotchas

- Odoo 18: `list` not `tree` in view arch; `groups_id` on res.users;
  salesman groups live in `sales_team`; sale.order has no
  `commercial_partner_id` (use `partner_invoice_id.commercial_partner_id`);
  res.company and res.users have no mail thread (warning channel is
  `mail.mail`).
- Tests: Odoo's `assertRaises` rolls back a savepoint on the expected
  exception, so writes asserted after a raise must use try/except; flush
  deferred INSERTs before partial unique index tests.
- The Fortnox rate limit default (25 calls / 5 s) still needs sandbox
  verification.
