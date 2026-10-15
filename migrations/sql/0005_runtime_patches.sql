ALTER TABLE budget_accounts ADD COLUMN IF NOT EXISTS tokens_held bigint NOT NULL DEFAULT 0 CHECK (tokens_held >= 0);

CREATE TABLE IF NOT EXISTS resource_heads (
  project_id uuid NOT NULL REFERENCES projects(id),
  resource_id text NOT NULL,
  version text NOT NULL,
  PRIMARY KEY (project_id, resource_id)
);
ALTER TABLE resource_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE resource_heads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS resource_heads_project_scope ON resource_heads;
CREATE POLICY resource_heads_project_scope ON resource_heads
  USING (project_id = nullif(current_setting('aether.project_id', true), '')::uuid)
  WITH CHECK (project_id = nullif(current_setting('aether.project_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON resource_heads TO aether_api, aether_worker;
