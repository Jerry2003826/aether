-- Least-privilege grants. Re-applied on existing databases, so broad grants are
-- revoked before the explicit list. New tables get nothing until a later
-- migration grants a specific action.

DO $$
BEGIN
  EXECUTE format(
    'REVOKE CONNECT ON DATABASE %I FROM PUBLIC',
    current_database()
  );
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO aether_api, aether_worker, aether_verifier, aether_dispatcher',
    current_database()
  );
END;
$$;

GRANT USAGE ON SCHEMA public TO aether_api, aether_worker, aether_verifier, aether_dispatcher;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, aether_api, aether_worker, aether_verifier, aether_dispatcher;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, aether_api, aether_worker, aether_verifier, aether_dispatcher;

ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, aether_api, aether_worker, aether_verifier, aether_dispatcher;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, aether_api, aether_worker, aether_verifier, aether_dispatcher;

-- Control plane: identity, projects, idempotency, artifacts, draft tasks, and
-- inserting the command the dispatcher will deliver. No DML on budget,
-- operations, audit, verifier evidence, or promotion scores.
GRANT SELECT, INSERT ON owners TO aether_api;
GRANT SELECT, INSERT, UPDATE ON auth_sessions TO aether_api;
GRANT SELECT, INSERT ON projects TO aether_api;
GRANT SELECT, INSERT, UPDATE ON idempotency_keys TO aether_api;
GRANT SELECT, INSERT, UPDATE ON artifacts TO aether_api;
GRANT SELECT, INSERT, UPDATE ON tasks TO aether_api;
GRANT SELECT, INSERT ON command_outbox TO aether_api;
GRANT INSERT ON task_events TO aether_api;
GRANT UPDATE ON approvals TO aether_api;

GRANT SELECT ON
  goal_contracts, task_nodes, node_dependencies, task_events, capability_grants,
  approvals, operations, verification_results, evidence_links, budget_accounts,
  model_requests, sources, source_versions, chunks, index_generations, memories,
  skills, skill_versions, evaluation_runs, skill_deployments, profile_snapshots,
  recommendation_impressions, feedback_events, watches, notifications, audit_events,
  ingestion_jobs, coding_jobs, connector_configs
TO aether_api;

-- Worker runs the task, budget, approval, and operation ledgers. It cannot
-- write verifier evidence or promotion scores.
GRANT SELECT, INSERT, UPDATE ON
  tasks, goal_contracts, task_nodes, node_dependencies, command_outbox, task_events,
  capability_grants, approvals, operations, artifacts, evidence_links,
  budget_accounts, model_requests, sources, source_versions, chunks, index_generations,
  memories, skills, skill_versions, skill_deployments, profile_snapshots,
  recommendation_impressions, feedback_events, watches, notifications,
  ingestion_jobs, coding_jobs, connector_configs
TO aether_worker;
GRANT SELECT, INSERT ON audit_events TO aether_worker;
GRANT SELECT ON verification_results, evaluation_runs, projects TO aether_worker;

-- Verifier appends evidence. Dispatcher only moves outbox rows.
GRANT SELECT ON
  projects, tasks, goal_contracts, task_nodes, node_dependencies, command_outbox,
  task_events, capability_grants, approvals, operations, artifacts,
  verification_results, evidence_links, budget_accounts, model_requests, sources,
  source_versions, chunks, index_generations, memories, skills, skill_versions,
  evaluation_runs, skill_deployments, profile_snapshots, recommendation_impressions,
  feedback_events, watches, notifications, audit_events, ingestion_jobs, coding_jobs,
  connector_configs
TO aether_verifier;
GRANT INSERT ON verification_results, evidence_links, artifacts TO aether_verifier;
GRANT SELECT, UPDATE ON command_outbox TO aether_dispatcher;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aether_api, aether_worker, aether_verifier;

DO $$
BEGIN
  IF to_regclass('public.resource_heads') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON resource_heads FROM aether_api, aether_worker';
    EXECUTE 'GRANT SELECT ON resource_heads TO aether_api';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE ON resource_heads TO aether_worker';
  END IF;
END;
$$;
