"""Shared setup for the system round 1 regressions in P1-04, P1-05, P1-06, P2-01, P2-05 and P2-07.

Only the provider transport is replaced. PostgreSQL, the artifact store, the verifier and Docker are real.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import litellm
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.artifacts import ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.goal_effect import WorkerEndpoints
from tests.integration.test_auth import ORIGIN, _bootstrap

KEY = "synthetic-audit-key"


def project(client, name: str = "审计项目") -> tuple[str, str, dict[str, str]]:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    created = client.post("/v1/projects", headers={**headers, "Idempotency-Key": f"audit-{uuid.uuid4().hex}"}, json={"name": name})
    assert created.status_code == 201, created.text
    return session["owner_id"], created.json()["id"], headers


def key() -> str:
    return f"audit-{uuid.uuid4().hex}"


def endpoints(role_urls: dict[str, str], root, *, provider: str = "live") -> WorkerEndpoints:
    return WorkerEndpoints(
        worker_url=role_urls["aether_worker"],
        verifier_url=role_urls["aether_verifier"],
        dispatcher_url=role_urls["aether_dispatcher"],
        artifact_root=root,
        temporal_address="127.0.0.1:9",
        task_queue=f"audit-{uuid.uuid4().hex}",
        research_provider=provider,
        live_model_base="http://never-contacted.invalid/v1",
        live_model_alias="audit-model",
        live_model_key=KEY,
    )


def fake_transport(monkeypatch, *, output: str, tokens: int = 100, completion_tokens: int = 20, cost: float | None = 0.001) -> dict[str, Any]:
    """Replace only litellm.completion. Every prompt that would leave the process is kept."""
    state: dict[str, Any] = {"calls": 0, "prompts": [], "output": output}

    def completion(**kwargs):
        state["calls"] += 1
        state["prompts"].append(kwargs["messages"][0]["content"])
        hidden = {} if cost is None else {"response_cost": cost}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=state["output"]))],
            model="audit-model",
            usage=SimpleNamespace(prompt_tokens=tokens, completion_tokens=completion_tokens),
            _hidden_params=hidden,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    return state


def store_for(client, role_urls: dict[str, str]) -> ArtifactStore:
    return ArtifactStore(client.app.state.settings.artifact_root, Database(role_urls["aether_worker"]))


def rows(database: Database, owner_id: str, project_id: str, sql: str, **params: Any) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def sending_reservation(database: Database, *, owner_id: str, project_id: str, prompt: str, alias: str, output_cap: int = 16) -> tuple[str, str]:
    """A running task whose single model request is reserved and already flipped to sending.

    This is the only state in which the gateway may forward a call to the provider.
    """
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_id = conn.execute(
            text("INSERT INTO tasks (project_id, original_goal, workflow_id, status) VALUES (:project_id, '网关调用', :workflow, 'RUNNING') RETURNING id::text"),
            {"project_id": project_id, "workflow": f"gateway-{uuid.uuid4()}"},
        ).scalar_one()
    ledger = BudgetLedger(database)
    ledger.open_account(owner_id=owner_id, project_id=project_id, task_id=task_id, limit_micro_usd=2_000_000, token_limit=100_000, max_requests=4)
    request_key = f"gateway-{uuid.uuid4().hex}"
    attempt = uuid.uuid4().hex
    prepared = ledger.prepare_model_call(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
        amount=10_000,
        role="research",
        tokens=64,
        model_alias=alias,
        prompt_sha256=sha256_text(prompt),
        output_cap=output_cap,
        source_snapshot_sha256=sha256_text(""),
        attempt_id=attempt,
    )
    assert prepared["action"] == "send", prepared
    assert ledger.acquire_dispatch_permit(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, attempt_id=attempt)
    assert ledger.consume_dispatch_permit(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, attempt_id=attempt)
    return task_id, request_key
