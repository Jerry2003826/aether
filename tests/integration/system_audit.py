"""Helpers for the system-audit regressions. Only the provider transport is faked."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import litellm
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.goal_effect import WorkerEndpoints, materialize_goal_effect
from tests.integration.test_auth import ORIGIN, _bootstrap

KEY = "synthetic-audit-key"
FACT = "事实：水在标准大气压下于一百摄氏度沸腾。"
FACT_GOAL = "请根据已上传资料回答沸点"


def project(client, name: str = "审计项目") -> tuple[str, str, dict[str, str]]:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    created = client.post("/v1/projects", headers={**headers, "Idempotency-Key": f"audit-{uuid.uuid4().hex}"}, json={"name": name})
    assert created.status_code == 201, created.text
    return session["owner_id"], created.json()["id"], headers


def second_project(client, headers: dict[str, str], name: str = "另一个项目") -> str:
    created = client.post("/v1/projects", headers={**headers, "Idempotency-Key": f"audit-{uuid.uuid4().hex}"}, json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["id"]


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


def worker_db(role_urls: dict[str, str]) -> Database:
    return Database(role_urls["aether_worker"])


def store_for(client, role_urls: dict[str, str]) -> ArtifactStore:
    return ArtifactStore(client.app.state.settings.artifact_root, worker_db(role_urls))


def scalar(database: Database, owner_id: str, project_id: str, sql: str, **params: Any) -> Any:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return conn.execute(text(sql), params).scalar()


def rows(database: Database, owner_id: str, project_id: str, sql: str, **params: Any) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def answered_task(client, role_urls: dict[str, str], *, delete_answer: bool) -> tuple[str, str, dict[str, str], str, WorkerEndpoints]:
    owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    scripted = endpoints(role_urls, store_for(client, role_urls).root, provider="scripted")
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": FACT_GOAL})
    assert created.status_code == 202, created.text
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    assert materialize_goal_effect(scripted, payload)["status"] == "SUCCEEDED"
    answer = rows(
        worker_db(role_urls),
        owner,
        project_id,
        "SELECT artifact_id::text AS id FROM task_artifacts WHERE task_id = :t AND classification = 'task_answer'",
        t=payload["task_id"],
    )[0]["id"]
    if delete_answer:
        deleted = client.delete(f"/v1/projects/{project_id}/artifacts/{answer}", headers={**headers, "Idempotency-Key": key()})
        assert deleted.status_code == 204, deleted.text
    return owner, project_id, headers, answer, scripted
