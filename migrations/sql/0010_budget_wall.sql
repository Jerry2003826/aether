-- Wall-clock budget for a task. Existing accounts keep the published default.
-- The worker resolves a gateway token without a project setting, so the token
-- hash policy has to exist outside the gateway test.

ALTER TABLE budget_accounts ADD COLUMN IF NOT EXISTS active_seconds integer NOT NULL DEFAULT 1800 CHECK (active_seconds >= 1);

DROP POLICY IF EXISTS capability_grants_token_lookup ON capability_grants;
CREATE POLICY capability_grants_token_lookup ON capability_grants
  FOR SELECT TO aether_worker
  USING (token_hash = nullif(current_setting('aether.token_hash', true), ''));
