-- Separate shared blobs from per-task artifact references, and record the
-- observation that a verification row actually checked.

CREATE TABLE IF NOT EXISTS task_artifacts (
  project_id uuid NOT NULL REFERENCES projects(id),
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  task_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  classification text NOT NULL,
  deleted_at timestamptz,
  PRIMARY KEY (project_id, id),
  FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, id),
  FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS task_artifacts_live_link
  ON task_artifacts (project_id, task_id, artifact_id, classification)
  WHERE deleted_at IS NULL;

ALTER TABLE task_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_artifacts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS task_artifacts_project_scope ON task_artifacts;
CREATE POLICY task_artifacts_project_scope ON task_artifacts
  USING (project_id = nullif(current_setting('aether.project_id', true), '')::uuid)
  WITH CHECK (project_id = nullif(current_setting('aether.project_id', true), '')::uuid);
DROP POLICY IF EXISTS task_artifacts_api_owner ON task_artifacts;
CREATE POLICY task_artifacts_api_owner ON task_artifacts
  AS RESTRICTIVE TO aether_api
  USING (aether_owner_visible(project_id))
  WITH CHECK (aether_owner_visible(project_id));

GRANT SELECT, INSERT, UPDATE ON task_artifacts TO aether_worker;
GRANT SELECT ON task_artifacts TO aether_api, aether_verifier;

ALTER TABLE verification_results ADD COLUMN IF NOT EXISTS observed_sha256 text;
ALTER TABLE verification_results ADD COLUMN IF NOT EXISTS source_snapshot_sha256 text;
ALTER TABLE verification_results ADD COLUMN IF NOT EXISTS verifier_version text;

ALTER TABLE verification_results DROP CONSTRAINT IF EXISTS verification_results_observed_sha256_check;
ALTER TABLE verification_results ADD CONSTRAINT verification_results_observed_sha256_check
  CHECK (observed_sha256 IS NULL OR observed_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE verification_results DROP CONSTRAINT IF EXISTS verification_results_source_snapshot_sha256_check;
ALTER TABLE verification_results ADD CONSTRAINT verification_results_source_snapshot_sha256_check
  CHECK (source_snapshot_sha256 IS NULL OR source_snapshot_sha256 ~ '^[0-9a-f]{64}$');
