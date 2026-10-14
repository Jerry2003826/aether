# Machine contracts, not implemented services

- domain.schema.json: generated from the supplied Pydantic domain models; draft 2020-12.
- acceptance.schema.json: strict parameter schemas selected by the trusted template registry.
- openapi.yaml: all designed external routes, request/response schemas and authentication/idempotency/version requirements. Codex must implement them and diff the real generated OpenAPI against this contract; shape compatibility alone is not security.
- schema.sql: initial PostgreSQL 17 + pgvector DDL contract. It has NOT been run on PostgreSQL here. F02/F03 must build migrations, runtime-role grants, immutability/retention controls, transactions and integration tests. JSONB reference fields additionally require service validation.
- interfaces.py: AETHER-owned ports, not upstream library signatures.

Reference frozen models are shallowly frozen. Persisted immutable contracts, reliable budgets and trusted evidence require the transaction and authority boundaries specified in docs. Readable fixture IDs are not production UUIDs.
