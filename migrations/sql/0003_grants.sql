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
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aether_api, aether_worker;
REVOKE INSERT, UPDATE, DELETE ON verification_results FROM aether_api;
REVOKE INSERT, UPDATE, DELETE ON evaluation_runs FROM aether_api;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aether_verifier, aether_dispatcher;
GRANT INSERT ON verification_results, evidence_links, artifacts TO aether_verifier;
GRANT UPDATE ON command_outbox TO aether_dispatcher;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aether_api, aether_worker, aether_verifier;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aether_api, aether_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO aether_verifier, aether_dispatcher;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO aether_api, aether_worker, aether_verifier;
