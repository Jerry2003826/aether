-- Logical artifacts may share one content-addressed blob. A tombstone does not
-- keep that blob from being written again under a new row. Live rows of the
-- same classification and media type still collapse to one id.

ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_project_id_storage_key_key;

CREATE UNIQUE INDEX IF NOT EXISTS artifacts_live_identity
  ON artifacts (project_id, storage_key, classification, media_type)
  WHERE deleted_at IS NULL;

UPDATE artifacts AS artifact
SET classification = 'memory_claim'
FROM memories AS memory
WHERE artifact.project_id = memory.project_id
  AND artifact.id = memory.claim_artifact_id
  AND artifact.classification = 'project_private';

UPDATE artifacts AS artifact
SET deleted_at = COALESCE(artifact.deleted_at, now())
FROM memories AS memory
WHERE artifact.project_id = memory.project_id
  AND artifact.id = memory.claim_artifact_id
  AND (memory.deleted_at IS NOT NULL OR memory.epistemic_status = 'superseded')
  AND artifact.deleted_at IS NULL;
