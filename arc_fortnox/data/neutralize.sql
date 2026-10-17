-- A neutralized copy of a database (staging, a test restore) must never
-- write to the live Fortnox books, and must never spend the live refresh
-- token: Fortnox invalidates the old one on every refresh, so a single call
-- from a copy disconnects production. Drop the tokens and the client
-- secret, and stop the automatic trigger. Authorize the copy again against
-- a Fortnox sandbox company if it needs the bridge.
UPDATE arc_fortnox_credential
   SET access_token = NULL,
       refresh_token = NULL,
       access_token_expires_at = NULL,
       state_nonce = NULL,
       state_nonce_expires_at = NULL,
       state = 'not_authorized',
       last_error = 'Neutralized database: tokens removed.';

UPDATE res_company
   SET arc_fortnox_client_secret = NULL,
       arc_fortnox_trigger_mode = 'manual';

DELETE FROM ir_config_parameter
 WHERE key IN ('arc_fortnox.client_secret', 'arc_fortnox_client_secret');
