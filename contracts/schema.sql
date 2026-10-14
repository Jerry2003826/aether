-- AETHER V1 PostgreSQL 17 + pgvector initial schema CONTRACT.
-- Not executed against PostgreSQL in this delivery. Convert to Alembic and test.
-- F02/F03 must add least-privilege runtime roles and validate using NON-superuser roles.
-- project context is set ONLY after authenticated authorization, per transaction.
-- Service accounts may not bypass RLS. Actor containers have no DB credentials.
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE owners (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 display_name text NOT NULL,
 password_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_owner_v1 ON owners ((true));
CREATE TABLE auth_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES owners(id),
 token_hash text UNIQUE NOT NULL CHECK(token_hash ~ '^[0-9a-f]{64}$'),
 csrf_secret_hash text NOT NULL, expires_at timestamptz NOT NULL,
 last_seen_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE projects (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES owners(id),
 name text NOT NULL CHECK(length(name)>0), version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 visibility_epoch bigint NOT NULL DEFAULT 1 CHECK(visibility_epoch>0),
 created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz
);
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;
CREATE POLICY projects_owner ON projects
 USING(owner_id = nullif(current_setting('aether.owner_id',true),'')::uuid)
 WITH CHECK(owner_id = nullif(current_setting('aether.owner_id',true),'')::uuid);


CREATE TABLE tasks (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 original_goal text NOT NULL, draft_contract jsonb,
status text NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','QUEUED','RUNNING','VERIFYING','WAITING_APPROVAL','PAUSED','NEEDS_REVIEW','SUCCEEDED','FAILED','CANCELLED')),
workflow_id text NOT NULL UNIQUE, parent_task_id uuid, current_contract_id uuid,
version bigint NOT NULL DEFAULT 1 CHECK(version>0), resume_state text,
projection_updated_at timestamptz, completed_at timestamptz,
FOREIGN KEY(project_id,parent_task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE goal_contracts (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, contract_version integer NOT NULL CHECK(contract_version>0),
content jsonb NOT NULL, contract_sha256 text NOT NULL CHECK(contract_sha256 ~ '^[0-9a-f]{64}$'),
approved_by uuid NOT NULL REFERENCES owners(id), approved_at timestamptz NOT NULL,
UNIQUE(project_id,task_id,contract_version), UNIQUE(project_id,task_id,id),
FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

ALTER TABLE tasks ADD FOREIGN KEY(project_id,id,current_contract_id) REFERENCES goal_contracts(project_id,task_id,id);

CREATE TABLE task_nodes (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, node_key text NOT NULL, role text NOT NULL,
objective text NOT NULL, state text NOT NULL, max_turns integer NOT NULL CHECK(max_turns BETWEEN 1 AND 64),
attempt integer NOT NULL DEFAULT 0 CHECK(attempt>=0), input_manifest jsonb, output_manifest jsonb,
UNIQUE(project_id,task_id,node_key), UNIQUE(project_id,task_id,id), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE node_dependencies (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, node_id uuid NOT NULL, dependency_id uuid NOT NULL,
CHECK(node_id<>dependency_id), UNIQUE(project_id,node_id,dependency_id),
FOREIGN KEY(project_id,task_id,node_id) REFERENCES task_nodes(project_id,task_id,id),
FOREIGN KEY(project_id,task_id,dependency_id) REFERENCES task_nodes(project_id,task_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE command_outbox (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid, command_key text NOT NULL, kind text NOT NULL, payload jsonb NOT NULL,
payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),
status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','DELIVERING','DELIVERED','FAILED')),
lease_until timestamptz, attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0), delivered_at timestamptz,
UNIQUE(project_id,command_key), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE task_events (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, sequence bigint NOT NULL CHECK(sequence>0), event_key text NOT NULL,
event_type text NOT NULL, payload jsonb NOT NULL, schema_version text NOT NULL DEFAULT '1.0',
UNIQUE(project_id,task_id,sequence), UNIQUE(project_id,task_id,event_key), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE capability_grants (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid, grantee_service text NOT NULL, token_hash text UNIQUE NOT NULL,
capability_type text NOT NULL, scope jsonb NOT NULL, expires_at timestamptz NOT NULL, revoked_at timestamptz,
policy_version text NOT NULL, FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE approvals (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, intent_digest text NOT NULL CHECK(intent_digest ~ '^[0-9a-f]{64}$'),
requested_payload jsonb NOT NULL, decision text NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','approved','rejected','expired')),
owner_id uuid NOT NULL REFERENCES owners(id), expires_at timestamptz NOT NULL, decided_at timestamptz,
consumed_by_operation_id uuid, version bigint NOT NULL DEFAULT 1 CHECK(version>0),
UNIQUE(project_id,task_id,id), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE operations (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, intent_key text NOT NULL, tool_id text NOT NULL,
args_sha256 text NOT NULL CHECK(args_sha256 ~ '^[0-9a-f]{64}$'), intent_digest text NOT NULL CHECK(intent_digest ~ '^[0-9a-f]{64}$'),
normalized_arguments jsonb NOT NULL, resource_id text NOT NULL, resource_version text NOT NULL,
idempotency_class text NOT NULL CHECK(idempotency_class IN ('read_only','idempotent_write','reconcilable_write','non_idempotent')),
risk text NOT NULL CHECK(risk IN ('R0','R1','R2','R3')),
capability_id uuid NOT NULL, approval_id uuid,
state text NOT NULL DEFAULT 'PREPARED' CHECK(state IN ('PREPARED','DISPATCHED','CONFIRMED','FAILED','UNKNOWN')),
provider_request_id text, provider_idempotency_key text, result jsonb, error_code text,
fencing_token bigint NOT NULL DEFAULT 0, lease_until timestamptz, dispatched_at timestamptz, resolved_at timestamptz,
UNIQUE(project_id,task_id,intent_key), UNIQUE(project_id,task_id,id),
FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id), FOREIGN KEY(project_id,capability_id) REFERENCES capability_grants(project_id,id),
FOREIGN KEY(project_id,task_id,approval_id) REFERENCES approvals(project_id,task_id,id),
 PRIMARY KEY(project_id,id)
);

ALTER TABLE approvals ADD FOREIGN KEY(project_id,task_id,consumed_by_operation_id) REFERENCES operations(project_id,task_id,id);

CREATE TABLE artifacts (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid, sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'), byte_size bigint NOT NULL CHECK(byte_size>=0),
media_type text NOT NULL, storage_key text NOT NULL, classification text NOT NULL,
manifest jsonb, deleted_at timestamptz, UNIQUE(project_id,storage_key), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE verification_results (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, contract_id uuid NOT NULL, artifact_id uuid NOT NULL,
criterion_id text NOT NULL, verdict text NOT NULL CHECK(verdict IN ('PASS','FAIL','UNVERIFIABLE','AWAITING_HUMAN')),
artifact_sha256 text NOT NULL CHECK(artifact_sha256 ~ '^[0-9a-f]{64}$'), contract_sha256 text NOT NULL CHECK(contract_sha256 ~ '^[0-9a-f]{64}$'),
verifier_identity text NOT NULL, report_artifact_id uuid NOT NULL, reason text NOT NULL, checked_at timestamptz NOT NULL,
FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id), FOREIGN KEY(project_id,task_id,contract_id) REFERENCES goal_contracts(project_id,task_id,id),
FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id), FOREIGN KEY(project_id,report_artifact_id) REFERENCES artifacts(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE evidence_links (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 verification_id uuid, source_artifact_id uuid NOT NULL, artifact_id uuid NOT NULL,
 relation text NOT NULL CHECK(relation IN ('supports','contradicts','derived_from','supersedes','tested_by')),
 provenance jsonb NOT NULL, CHECK(relation<>'tested_by' OR verification_id IS NOT NULL),
 FOREIGN KEY(project_id,source_artifact_id) REFERENCES artifacts(project_id,id),
FOREIGN KEY(project_id,verification_id) REFERENCES verification_results(project_id,id), FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE budget_accounts (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, limit_micro_usd bigint NOT NULL CHECK(limit_micro_usd>=0),
spent_micro_usd bigint NOT NULL DEFAULT 0 CHECK(spent_micro_usd>=0),
held_micro_usd bigint NOT NULL DEFAULT 0 CHECK(held_micro_usd>=0),
token_limit bigint NOT NULL CHECK(token_limit>0), tokens_used bigint NOT NULL DEFAULT 0 CHECK(tokens_used>=0),
max_requests integer NOT NULL CHECK(max_requests>0), requests_admitted integer NOT NULL DEFAULT 0 CHECK(requests_admitted>=0),
verification_reserve_fraction numeric NOT NULL DEFAULT 0.2 CHECK(verification_reserve_fraction>=0 AND verification_reserve_fraction<1),
version bigint NOT NULL DEFAULT 1, UNIQUE(project_id,task_id), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE model_requests (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, request_key text NOT NULL, request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
role text NOT NULL, model_alias text NOT NULL, model_revision text, pricing_version text,
reserved_micro_usd bigint NOT NULL CHECK(reserved_micro_usd>=0), actual_micro_usd bigint CHECK(actual_micro_usd>=0),
input_tokens bigint CHECK(input_tokens>=0), output_tokens bigint CHECK(output_tokens>=0), usage_detail jsonb,
usage_status text NOT NULL CHECK(usage_status IN ('reserved','known','unknown','scripted','cancelled_before_dispatch')),
provider_request_id text, completed_at timestamptz,
CHECK(usage_status<>'known' OR (actual_micro_usd IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL)),
CHECK(usage_status<>'unknown' OR actual_micro_usd IS NULL),
UNIQUE(project_id,task_id,request_key), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE sources (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 kind text NOT NULL, locator text NOT NULL, canonical_locator_hash text NOT NULL,
approved_config jsonb NOT NULL, version bigint NOT NULL DEFAULT 1 CHECK(version>0),
status text NOT NULL, deleted_at timestamptz, UNIQUE(project_id,canonical_locator_hash),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE source_versions (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 source_id uuid NOT NULL, raw_artifact_id uuid NOT NULL, canonical_artifact_id uuid NOT NULL,
content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'), parser_version text NOT NULL,
fetched_at timestamptz NOT NULL, published_at timestamptz, etag text, metadata jsonb NOT NULL DEFAULT '{}',
UNIQUE(project_id,source_id,content_sha256,parser_version), FOREIGN KEY(project_id,source_id) REFERENCES sources(project_id,id),
FOREIGN KEY(project_id,raw_artifact_id) REFERENCES artifacts(project_id,id), FOREIGN KEY(project_id,canonical_artifact_id) REFERENCES artifacts(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE chunks (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 source_version_id uuid NOT NULL, ordinal integer NOT NULL CHECK(ordinal>=0), text_content text NOT NULL,
start_offset bigint NOT NULL CHECK(start_offset>=0), end_offset bigint NOT NULL CHECK(end_offset>start_offset),
page integer CHECK(page>=1), chunker_version text NOT NULL, embedding_revision text,
embedding vector(1024), deleted_at timestamptz, UNIQUE(project_id,source_version_id,ordinal,chunker_version),
FOREIGN KEY(project_id,source_version_id) REFERENCES source_versions(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE index_generations (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 generation_key text NOT NULL, tokenizer_version text NOT NULL, source_watermark bigint NOT NULL,
manifest_artifact_id uuid NOT NULL, state text NOT NULL CHECK(state IN ('BUILDING','READY','ACTIVE','RETIRED','FAILED')),
manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'), activated_at timestamptz,
UNIQUE(project_id,generation_key), FOREIGN KEY(project_id,manifest_artifact_id) REFERENCES artifacts(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE UNIQUE INDEX one_active_lexical_generation_per_project ON index_generations(project_id) WHERE state='ACTIVE';

CREATE TABLE memories (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 memory_key text NOT NULL, revision integer NOT NULL CHECK(revision>0), kind text NOT NULL,
claim text NOT NULL, category text NOT NULL, claim_artifact_id uuid NOT NULL,
 FOREIGN KEY(project_id,claim_artifact_id) REFERENCES artifacts(project_id,id),
 provenance jsonb NOT NULL, confidence numeric CHECK(confidence BETWEEN 0 AND 1),
epistemic_status text NOT NULL CHECK(epistemic_status IN ('proposed','source_claim','user_confirmed','observed_test','inference','superseded')),
valid_from timestamptz, valid_to timestamptz, supersedes_id uuid, conflict_group uuid, deleted_at timestamptz,
UNIQUE(project_id,memory_key,revision), FOREIGN KEY(project_id,supersedes_id) REFERENCES memories(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE skills (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 name text NOT NULL, description text NOT NULL, deleted_at timestamptz, UNIQUE(project_id,name),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE skill_versions (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 skill_id uuid NOT NULL, version integer NOT NULL CHECK(version>0), parent_version_id uuid,
bundle_artifact_id uuid NOT NULL, bundle_sha256 text NOT NULL CHECK(bundle_sha256 ~ '^[0-9a-f]{64}$'),
manifest jsonb NOT NULL, status text NOT NULL CHECK(status IN ('DRAFT','QUARANTINED','STATIC_PASSED','EVALUATING','ELIGIBLE','CANARY','ACTIVE','REJECTED','ROLLED_BACK')),
UNIQUE(project_id,skill_id,version), UNIQUE(project_id,skill_id,id),
FOREIGN KEY(project_id,skill_id) REFERENCES skills(project_id,id), FOREIGN KEY(project_id,bundle_artifact_id) REFERENCES artifacts(project_id,id),
FOREIGN KEY(project_id,skill_id,parent_version_id) REFERENCES skill_versions(project_id,skill_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE evaluation_runs (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 purpose text NOT NULL CHECK(purpose IN ('development','validation','release')),
dataset_manifest_sha256 text NOT NULL CHECK(dataset_manifest_sha256 ~ '^[0-9a-f]{64}$'), model_manifest jsonb NOT NULL,
candidate_version_id uuid, baseline_version_id uuid, configuration jsonb NOT NULL,
status text NOT NULL, metrics jsonb, report_artifact_id uuid, evaluator_identity text,
FOREIGN KEY(project_id,candidate_version_id) REFERENCES skill_versions(project_id,id), FOREIGN KEY(project_id,baseline_version_id) REFERENCES skill_versions(project_id,id), FOREIGN KEY(project_id,report_artifact_id) REFERENCES artifacts(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE skill_deployments (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 skill_id uuid NOT NULL, slot text NOT NULL CHECK(slot IN ('active','canary')),
version_id uuid NOT NULL, evaluation_id uuid NOT NULL, approval_id uuid NOT NULL,
deployment_version bigint NOT NULL CHECK(deployment_version>0), traffic_fraction numeric NOT NULL CHECK(traffic_fraction BETWEEN 0 AND 1),
UNIQUE(project_id,skill_id,slot), FOREIGN KEY(project_id,skill_id) REFERENCES skills(project_id,id),
FOREIGN KEY(project_id,skill_id,version_id) REFERENCES skill_versions(project_id,skill_id,id),
FOREIGN KEY(project_id,evaluation_id) REFERENCES evaluation_runs(project_id,id), FOREIGN KEY(project_id,approval_id) REFERENCES approvals(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE profile_snapshots (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 profile_version bigint NOT NULL CHECK(profile_version>0), feedback_watermark bigint NOT NULL,
feature_config jsonb NOT NULL, long_vector vector(1024), short_vector vector(1024),
embedding_revision text, UNIQUE(project_id,profile_version),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE recommendation_impressions (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 profile_id uuid NOT NULL, policy_version text NOT NULL, candidates jsonb NOT NULL,
displayed_items jsonb NOT NULL, propensity_details jsonb NOT NULL, index_generation_id uuid,
FOREIGN KEY(project_id,profile_id) REFERENCES profile_snapshots(project_id,id), FOREIGN KEY(project_id,index_generation_id) REFERENCES index_generations(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE feedback_events (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 event_key text NOT NULL, impression_id uuid, source_id uuid NOT NULL,
kind text NOT NULL CHECK(kind IN ('click','save','helpful','dwell','hide','unsave')),
dwell_seconds numeric CHECK(dwell_seconds>=0), event_time timestamptz NOT NULL, deleted_at timestamptz,
UNIQUE(project_id,event_key), FOREIGN KEY(project_id,impression_id) REFERENCES recommendation_impressions(project_id,id), FOREIGN KEY(project_id,source_id) REFERENCES sources(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE watches (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 purpose text NOT NULL, schedule_id text NOT NULL UNIQUE, source_ids jsonb NOT NULL,
schedule_spec jsonb NOT NULL, budget_spec jsonb NOT NULL, approved_by uuid REFERENCES owners(id),
status text NOT NULL CHECK(status IN ('DRAFT','ACTIVE','PAUSED','DELETED')),
version bigint NOT NULL DEFAULT 1, last_watermark jsonb, last_run_at timestamptz,
 PRIMARY KEY(project_id,id)
);

CREATE TABLE notifications (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 watch_id uuid, task_id uuid, dedup_key text NOT NULL, title text NOT NULL, content jsonb NOT NULL,
read_at timestamptz, UNIQUE(project_id,dedup_key), FOREIGN KEY(project_id,watch_id) REFERENCES watches(project_id,id), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE audit_events (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 actor_identity text NOT NULL, action text NOT NULL, resource_type text NOT NULL, resource_id text NOT NULL,
request_id text NOT NULL, payload_redacted jsonb NOT NULL,
previous_hash text, event_hash text NOT NULL, sequence bigint NOT NULL CHECK(sequence>0),
UNIQUE(project_id,sequence),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE ingestion_jobs (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 source_id uuid NOT NULL, command_id uuid NOT NULL, status text NOT NULL,
source_version_id uuid, error_code text, finished_at timestamptz,
FOREIGN KEY(project_id,source_id) REFERENCES sources(project_id,id), FOREIGN KEY(project_id,command_id) REFERENCES command_outbox(project_id,id), FOREIGN KEY(project_id,source_version_id) REFERENCES source_versions(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE coding_jobs (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 task_id uuid NOT NULL, operation_id uuid NOT NULL, job_key text NOT NULL,
state text NOT NULL CHECK(state IN ('QUEUED','STARTING','ACTIVE','EXPORTING','FINISHED','FAILED','CANCELLED')),
container_reference text, conversation_reference text, base_artifact_id uuid NOT NULL,
result_artifact_id uuid, patch_artifact_id uuid, events_artifact_id uuid,
deadline timestamptz NOT NULL, last_heartbeat_at timestamptz, fencing_token bigint NOT NULL DEFAULT 0,
UNIQUE(project_id,task_id,job_key),
FOREIGN KEY(project_id,task_id,operation_id) REFERENCES operations(project_id,task_id,id),
FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,id), FOREIGN KEY(project_id,base_artifact_id) REFERENCES artifacts(project_id,id), FOREIGN KEY(project_id,result_artifact_id) REFERENCES artifacts(project_id,id),
FOREIGN KEY(project_id,patch_artifact_id) REFERENCES artifacts(project_id,id), FOREIGN KEY(project_id,events_artifact_id) REFERENCES artifacts(project_id,id),
 PRIMARY KEY(project_id,id)
);

CREATE TABLE connector_configs (
 project_id uuid NOT NULL REFERENCES projects(id),
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT now(),
 connector_key text NOT NULL, transport text NOT NULL, reviewed_manifest jsonb NOT NULL,
manifest_sha256 text NOT NULL, secret_reference text, status text NOT NULL,
version bigint NOT NULL DEFAULT 1, UNIQUE(project_id,connector_key),
 PRIMARY KEY(project_id,id)
);

ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tasks_project_scope ON tasks
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE goal_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_contracts FORCE ROW LEVEL SECURITY;
CREATE POLICY goal_contracts_project_scope ON goal_contracts
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE task_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_nodes FORCE ROW LEVEL SECURITY;
CREATE POLICY task_nodes_project_scope ON task_nodes
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE node_dependencies ENABLE ROW LEVEL SECURITY;
ALTER TABLE node_dependencies FORCE ROW LEVEL SECURITY;
CREATE POLICY node_dependencies_project_scope ON node_dependencies
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE command_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE command_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY command_outbox_project_scope ON command_outbox
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE task_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_events FORCE ROW LEVEL SECURITY;
CREATE POLICY task_events_project_scope ON task_events
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE capability_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE capability_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY capability_grants_project_scope ON capability_grants
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY approvals_project_scope ON approvals
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE operations FORCE ROW LEVEL SECURITY;
CREATE POLICY operations_project_scope ON operations
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY artifacts_project_scope ON artifacts
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE verification_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE verification_results FORCE ROW LEVEL SECURITY;
CREATE POLICY verification_results_project_scope ON verification_results
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE evidence_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_links FORCE ROW LEVEL SECURITY;
CREATE POLICY evidence_links_project_scope ON evidence_links
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE budget_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_accounts FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_accounts_project_scope ON budget_accounts
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE model_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY model_requests_project_scope ON model_requests
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources FORCE ROW LEVEL SECURITY;
CREATE POLICY sources_project_scope ON sources
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE source_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY source_versions_project_scope ON source_versions
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY chunks_project_scope ON chunks
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE index_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY index_generations_project_scope ON index_generations
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;
CREATE POLICY memories_project_scope ON memories
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE skills ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills FORCE ROW LEVEL SECURITY;
CREATE POLICY skills_project_scope ON skills
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE skill_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY skill_versions_project_scope ON skill_versions
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE evaluation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluation_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY evaluation_runs_project_scope ON evaluation_runs
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE skill_deployments ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_deployments FORCE ROW LEVEL SECURITY;
CREATE POLICY skill_deployments_project_scope ON skill_deployments
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE profile_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE profile_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY profile_snapshots_project_scope ON profile_snapshots
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE recommendation_impressions ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendation_impressions FORCE ROW LEVEL SECURITY;
CREATE POLICY recommendation_impressions_project_scope ON recommendation_impressions
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE feedback_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback_events FORCE ROW LEVEL SECURITY;
CREATE POLICY feedback_events_project_scope ON feedback_events
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE watches ENABLE ROW LEVEL SECURITY;
ALTER TABLE watches FORCE ROW LEVEL SECURITY;
CREATE POLICY watches_project_scope ON watches
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;
CREATE POLICY notifications_project_scope ON notifications
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_events_project_scope ON audit_events
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE ingestion_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY ingestion_jobs_project_scope ON ingestion_jobs
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE coding_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE coding_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY coding_jobs_project_scope ON coding_jobs
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);

ALTER TABLE connector_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_configs FORCE ROW LEVEL SECURITY;
CREATE POLICY connector_configs_project_scope ON connector_configs
 USING(project_id=nullif(current_setting('aether.project_id',true),'')::uuid)
 WITH CHECK(project_id=nullif(current_setting('aether.project_id',true),'')::uuid);


-- Operational indexes; small-corpus vector search is exact by design.
-- No global HNSW index is enabled until project-filter recall is measured.
CREATE INDEX task_status_projection ON tasks(project_id,status,created_at);
CREATE INDEX pending_commands ON command_outbox(status,created_at) WHERE status IN ('PENDING','DELIVERING');
CREATE INDEX pending_operations ON operations(project_id,state) WHERE state IN ('DISPATCHED','UNKNOWN');
CREATE INDEX chunks_by_source_version ON chunks(project_id,source_version_id) WHERE deleted_at IS NULL;
CREATE INDEX models_by_task ON model_requests(project_id,task_id,created_at);
CREATE INDEX sources_visible ON sources(project_id,status) WHERE deleted_at IS NULL;
CREATE INDEX audit_by_resource ON audit_events(project_id,resource_type,resource_id,sequence);

-- Immutable contract bytes after approval. Draft edits live in tasks.draft_contract.
CREATE FUNCTION reject_contract_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Approved contracts are immutable; create a new approved version'; END;
$$;
CREATE TRIGGER approved_contract_immutable BEFORE UPDATE ON goal_contracts
 FOR EACH ROW EXECUTE FUNCTION reject_contract_update();

-- F02/F03 REQUIRED (not silently assumed):
-- 1. Runtime role GRANTs: API cannot write verifier evidence, promotion scores or final task state.
-- 2. Dispatcher globally lists pending rows through a narrow trusted interface, not an actor role.
-- 3. Domain services enforce cross-resource authorization; RLS is not a substitute for it.
-- 4. Immutable version/artifact/evidence/audit update restrictions; controlled retention cleanup.
-- 5. Row-lock/CAS transactions for approval consumption, budgets and skill deployments.
-- 6. Validate JSONB manifests against versioned schemas before writes; source_ids within watches
--    and provenance references cannot use composite FKs inside JSONB, so enforce in service tests.
-- 7. Add an append-only consumption/history table or transactional audit for operations/deployments.
-- 8. Secrets are stored outside this DB in server-owned secret storage; only refs/hashes here.
-- 9. Durable pricing/token sub-ledgers and trace retention consistent with configured provider billing.
COMMIT;
