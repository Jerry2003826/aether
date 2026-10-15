DROP POLICY IF EXISTS capability_grants_token_lookup ON capability_grants;
CREATE POLICY capability_grants_token_lookup ON capability_grants
  FOR SELECT TO aether_worker
  USING (token_hash = nullif(current_setting('aether.token_hash', true), ''));
