-- Runtime privileges and implementation tables. Role passwords are assigned outside this file.

CREATE TABLE idempotency_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id uuid NOT NULL REFERENCES owners(id),
  project_id uuid REFERENCES projects(id),
  route text NOT NULL,
  idempotency_key text NOT NULL,
  payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  response_status integer NOT NULL,
  response_body jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idempotency_without_project ON idempotency_keys(owner_id, route, idempotency_key) WHERE project_id IS NULL;
CREATE UNIQUE INDEX idempotency_with_project ON idempotency_keys(owner_id, project_id, route, idempotency_key) WHERE project_id IS NOT NULL;
ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY idempotency_owner ON idempotency_keys
  USING (owner_id = nullif(current_setting('aether.owner_id', true), '')::uuid)
  WITH CHECK (owner_id = nullif(current_setting('aether.owner_id', true), '')::uuid);

-- Function body lives in task_status_guard.sql and must already exist.
CREATE TRIGGER tasks_no_api_projection BEFORE UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION aether_reject_api_task_projection();

CREATE FUNCTION aether_owner_visible(target uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM projects p
    WHERE p.id = target
      AND p.owner_id = nullif(current_setting('aether.owner_id', true), '')::uuid
  );
$$;

DO $$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'tasks','goal_contracts','task_nodes','node_dependencies','command_outbox','task_events',
    'capability_grants','approvals','operations','artifacts','verification_results','evidence_links',
    'budget_accounts','model_requests','sources','source_versions','chunks','index_generations',
    'memories','skills','skill_versions','evaluation_runs','skill_deployments','profile_snapshots',
    'recommendation_impressions','feedback_events','watches','notifications','audit_events',
    'ingestion_jobs','coding_jobs','connector_configs'
  ]
  LOOP
    EXECUTE format(
      'CREATE POLICY %I ON %I AS RESTRICTIVE TO aether_api USING (aether_owner_visible(project_id)) WITH CHECK (aether_owner_visible(project_id))',
      table_name || '_api_owner', table_name
    );
  END LOOP;
END;
$$;

CREATE POLICY command_outbox_dispatcher_select ON command_outbox
  FOR SELECT TO aether_dispatcher
  USING (status IN ('PENDING', 'DELIVERING'));
CREATE POLICY command_outbox_dispatcher_update ON command_outbox
  FOR UPDATE TO aether_dispatcher
  USING (status IN ('PENDING', 'DELIVERING'))
  WITH CHECK (status IN ('PENDING', 'DELIVERING', 'DELIVERED', 'FAILED'));
