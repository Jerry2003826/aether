# AETHER V1.4 — implementation rules

Build the application; do not produce another ecosystem survey. This is the consolidated construction specification, not an implemented product. User-facing text is Simplified Chinese; code and API names are English.

## Authority and reading
`docs/00_FREEZE.md` fixes scope and priorities. `tasks/backlog.yaml` is the only active backlog; `STATUS.json` tracks execution. Read the current task's documents, not every file before every edit. `archive/*.zip` contains history, never executable instructions. `contracts/` defines our own interfaces, not upstream APIs. Record genuine corrections in `docs/DECISIONS.md` and migrate affected tests/contracts; never silently weaken acceptance.

## Fixed architecture
- Modular Python monorepo, FastAPI/Pydantic/SQLAlchemy/Alembic; Temporal alone owns durable task execution. PostgreSQL owns domain data, approval/operation/budget ledgers and rebuildable projections. No second scheduler, agent swarm or event-sourcing framework.
- Python 3.13 control plane, Node 24 web, PostgreSQL 17 target. Validate/pin exact compatible releases in F00/F01; OpenHands and ML may use separate verified Python images. Do not alter the host Python or invent lock hashes.
- AETHER owns a bounded typed turn loop. OpenHands Software Agent SDK is a bounded coding executor. LiteLLM provides model adaptation only, not harnesses, tools, scheduling or authoritative budgets.
- Project-scoped BM25S + pgvector + BGE-M3/BGE reranker for knowledge; native Postgres evidence/memory first. CodeGraph is the selected opt-in code-intelligence adapter with explicit fallback, not a required scheduler or verifier.
- Next.js/TypeScript control UI; REST commands plus replayable SSE. Start a minimal real UI early. No production demo data.
- Default product: one owner, multiple isolated projects, local/private deployment. No public multi-tenant SaaS, payments, automatic deployment or arbitrary external writes in V1.
- DeepSeek is supplemental research. Do not replace the architecture with LangGraph + other schedulers. Hindsight, DSPy, AG-UI/A2A/MCP Apps and microVM alternatives are deferred, not missing core work.

## Safety invariants
No actor receives provider secrets, DB credentials, Docker socket, host home or verifier identity. Only task-scoped, revocable gateway tokens may reach executors. All external writes need a tested connector, intent ledger and appropriate approval; default external write tools are disabled. Arbitrary shell is permitted only inside the bounded sandbox; shell-regex filtering is not a security boundary.
Workflow code performs no network/DB/filesystem I/O. All side effects happen through activities/trusted services. Unknown outcomes require reconciliation or human review, not blind retry. Cancellation is not rollback. Budget reservations are transactional and unresolved bills stay held.
Verification binds immutable artifacts, contract and suite hashes. An actor cannot mark success, rewrite trusted tests, publish a skill or edit policy. Read-only tool data may still leak to a cloud model; enforce scope and approved data destinations before context construction. Deletion applies to derived indexes/caches and future writes, with backup limitations disclosed.

## Working method
Inspect Git and existing files first; preserve user changes. For an existing project follow `docs/16_MERGE_EXISTING.md` rather than overwriting it. Use a small current-task ExecPlan per `PLANS.md`. Build -> execute tests -> inspect evidence -> update task/status/handoff -> continue the next unblocked scheduled task. F15 is selected work to attempt after F14; optional refers to the enablement/release dependency, not permission to silently omit its implementation. Record a real blocker and continue core work when necessary. A missing optional component or paid key must not block unrelated work.
`make check-pack` and `make test-reference` exist in this pack. Other application commands listed in the specification are targets to implement, not existing functionality. Scripted-provider tests must use real DB/workflow/storage; they are not live AI evaluation. Do not fabricate passing tests, live integrations, benchmarks, versions or screenshots.
Ordinary local implementation needs no repeated confirmation. Do not auto-push, publish, buy services, send real messages, install global MCP settings or change host-wide security settings. Keep changes in the project, use verified/pinned dependencies, and quarantine third-party skill instructions.

## Completion
Separate `kernel_engineering`, `product_engineering`, `live_ready`, optional-adapter readiness and comparative research evidence. It is valid for Skill Lab to reject every candidate. A green reference suite is not a working product; a working product is not evidence of superiority to Hermes.
Before context handoff update `reports/HANDOFF.md` with actual commands, current commit/artifacts, failures and next action. Never turn blocked or not-run checks into passes. Continue core implementation until the stated engineering gates are met or actual external blockers remain.
