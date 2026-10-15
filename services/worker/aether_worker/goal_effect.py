"""Turn an accepted task start into a durable artifact and an independent verification.

The activity is idempotent. A worker restart or a Temporal retry must not add a second
artifact, contract, or verification row for the same task.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from temporalio import activity

from aether_domain.security import sha256_text
from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.db import Database
from aether_worker.research import record_verification, scripted_answer

CRITERION_ID = "goal_artifact"


@dataclass(frozen=True)
class WorkerEndpoints:
    worker_url: str
    verifier_url: str
    dispatcher_url: str
    artifact_root: Path
    temporal_address: str
    task_queue: str


ENDPOINTS: WorkerEndpoints | None = None


def configure(endpoints: WorkerEndpoints) -> None:
    global ENDPOINTS
    ENDPOINTS = endpoints


def goal_document(task_id: str, goal: str) -> bytes:
    return json.dumps({"goal": goal, "task_id": task_id}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@activity.defn
async def prepare_task(payload: dict) -> dict[str, str]:
    if ENDPOINTS is None:
        raise RuntimeError("worker endpoints are not configured")
    return prepare_task_effect(ENDPOINTS, payload)


@activity.defn
async def execute_task(payload: dict) -> dict[str, str]:
    if ENDPOINTS is None:
        raise RuntimeError("worker endpoints are not configured")
    return execute_task_effect(ENDPOINTS, payload)


@activity.defn
async def materialize_goal(payload: dict) -> dict[str, str]:
    prepared = await prepare_task(payload)
    if prepared.get("status") != "prepared":
        return prepared
    return await execute_task(payload)


def prepare_task_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    command = _start_command(endpoints, payload)
    if command is None:
        return {"status": "ignored"}
    owner_id, project_id, task_id, goal = command
    worker = Database(endpoints.worker_url)
    store = ArtifactStore(endpoints.artifact_root, worker)
    _ensure_contract(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal)
    _ensure_artifact(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        classification="task_goal",
        data=goal_document(task_id, goal),
        media_type="text/plain",
    )
    _set_status(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, status="RUNNING", allowed=("QUEUED",))
    _event(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, event_key=f"running-{task_id}", event_type="task.running", payload={"status": "RUNNING"})
    return {"status": "prepared", "task_id": task_id}


def execute_task_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    command = _start_command(endpoints, payload)
    if command is None:
        return {"status": "ignored"}
    owner_id, project_id, task_id, goal = command
    worker = Database(endpoints.worker_url)
    store = ArtifactStore(endpoints.artifact_root, worker)
    status = _task_status(worker, owner_id, project_id, task_id)
    if status in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW", "CANCELLED"}:
        return {"status": status, "task_id": task_id}
    sources = _source_texts(worker, store, owner_id, project_id)
    if not sources:
        _set_status(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, status="NEEDS_REVIEW", allowed=("QUEUED", "RUNNING", "VERIFYING"))
        return {"status": "NEEDS_REVIEW", "task_id": task_id}
    answer = _ensure_answer(worker, store, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal, sources=sources)
    if not answer:
        _set_status(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, status="FAILED", allowed=("QUEUED", "RUNNING", "VERIFYING"))
        return {"status": "FAILED", "task_id": task_id}
    _set_status(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, status="VERIFYING", allowed=("RUNNING", "VERIFYING"))
    contract_id, contract_sha = _ensure_contract(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal)
    verification_id = _ensure_verification(
        endpoints,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        contract_id=contract_id,
        contract_sha=contract_sha,
        artifact_id=answer,
        goal=goal,
    )
    verdict = _verdict(endpoints, owner_id, project_id, verification_id)
    terminal = "SUCCEEDED" if verdict == "PASS" else "FAILED"
    _set_status(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, status=terminal, allowed=("VERIFYING", "RUNNING"))
    return {"status": terminal, "task_id": task_id, "verification_id": verification_id, "artifact_id": answer}


def _start_command(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> tuple[str, str, str, str] | None:
    project_id = str(payload.get("project_id") or "")
    task_id = str(payload.get("task_id") or "")
    command_id = str(payload.get("command_id") or "")
    if not project_id or not task_id or not command_id:
        return None
    worker = Database(endpoints.worker_url)
    command = _command(worker, project_id, command_id)
    if command is None or str(command["kind"]) != "start":
        return None
    body = command["payload"] if isinstance(command["payload"], dict) else json.loads(command["payload"])
    owner_id = str(body.get("owner_id") or "")
    goal = str(body.get("goal") or "")
    if not owner_id or not goal:
        raise RuntimeError("start command is missing owner_id or goal")
    return owner_id, project_id, task_id, goal


def materialize_goal_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    prepared = prepare_task_effect(endpoints, payload)
    if prepared.get("status") != "prepared":
        return prepared
    return execute_task_effect(endpoints, payload)


def _command(database: Database, project_id: str, command_id: str) -> dict[str, Any] | None:
    with database.transaction(project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT kind, payload FROM command_outbox WHERE project_id = :project_id AND command_key = :command_id"),
            {"project_id": project_id, "command_id": command_id},
        ).mappings().first()
    return None if row is None else dict(row)


def _ensure_contract(database: Database, *, owner_id: str, project_id: str, task_id: str, goal: str) -> tuple[str, str]:
    content = {"criterion_id": CRITERION_ID, "goal": goal}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"))
    digest = sha256_text(raw)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text(
                "SELECT id::text AS id, contract_sha256 FROM goal_contracts "
                "WHERE project_id = :project_id AND task_id = :task_id ORDER BY contract_version LIMIT 1"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().first()
        if existing is not None:
            return str(existing["id"]), str(existing["contract_sha256"])
        contract_id = conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, CAST(:content AS jsonb), :digest, :owner, now()) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "content": raw, "digest": digest, "owner": owner_id},
        ).scalar_one()
    return str(contract_id), digest


def _ensure_artifact(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    classification: str,
    data: bytes,
    media_type: str,
) -> dict[str, str]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text(
                "SELECT id::text AS id, sha256 FROM artifacts "
                "WHERE project_id = :project_id AND task_id = :task_id AND classification = :classification AND deleted_at IS NULL"
            ),
            {"project_id": project_id, "task_id": task_id, "classification": classification},
        ).mappings().first()
    if existing is not None:
        return {"id": str(existing["id"]), "sha256": str(existing["sha256"])}
    stored = store.put(owner_id=owner_id, project_id=project_id, data=data, media_type=media_type, classification=classification)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE artifacts SET task_id = :task_id "
                "WHERE project_id = :project_id AND id = :id AND task_id IS NULL AND deleted_at IS NULL"
            ),
            {"task_id": task_id, "project_id": project_id, "id": stored["artifact_id"]},
        )
    return {"id": str(stored["artifact_id"]), "sha256": str(stored["sha256"])}


def _ensure_verification(
    endpoints: WorkerEndpoints,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    contract_id: str,
    contract_sha: str,
    artifact_id: str,
    goal: str,
) -> str:
    verifier = Database(endpoints.verifier_url)
    store = ArtifactStore(endpoints.artifact_root, verifier)
    ledger, deleted = _artifact_ledger(verifier, owner_id, project_id, artifact_id)
    observed, readable = _observe_bytes(store, owner_id, project_id, artifact_id)
    sources = _source_texts(verifier, store, owner_id, project_id)
    checks_passed = (not deleted) and readable is not None and _answer_uses_source(readable, goal, sources)
    if ledger is None:
        ledger = observed
        checks_passed = False
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text(
                "SELECT id::text FROM verification_results "
                "WHERE project_id = :project_id AND task_id = :task_id AND criterion_id = :criterion_id "
                "AND artifact_id = :artifact_id AND contract_id = :contract_id "
                "AND artifact_sha256 = :artifact_sha AND contract_sha256 = :contract_sha"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "criterion_id": CRITERION_ID,
                "artifact_id": artifact_id,
                "contract_id": contract_id,
                "artifact_sha": ledger,
                "contract_sha": contract_sha,
            },
        ).scalar()
    if existing is not None:
        return str(existing)
    report = store.put(
        owner_id=owner_id,
        project_id=project_id,
        data=json.dumps({"criterion_id": CRITERION_ID, "observed_sha256": observed, "artifact_id": artifact_id}, sort_keys=True).encode(),
        media_type="text/plain",
        classification="task_verification_report",
    )
    now = datetime.now(UTC)
    recorded = record_verification(
        verifier,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        contract_id=contract_id,
        artifact_id=artifact_id,
        report_artifact_id=str(report["artifact_id"]),
        criterion_id=CRITERION_ID,
        artifact_sha256=ledger,
        observed_sha256=observed,
        contract_sha256=contract_sha,
        expires_at=now + timedelta(hours=24),
        now=now,
        checks_passed=checks_passed and observed == ledger,
    )
    return str(recorded["id"])


def _source_texts(database: Database, store: ArtifactStore, owner_id: str, project_id: str) -> list[str]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT id::text AS id FROM artifacts "
                "WHERE project_id = :project_id AND deleted_at IS NULL AND task_id IS NULL "
                "AND media_type = 'text/plain' AND classification = 'project_private' "
                "ORDER BY created_at, id"
            ),
            {"project_id": project_id},
        ).scalars().all()
    texts: list[str] = []
    for artifact_id in rows:
        try:
            data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(artifact_id))
        except ArtifactError:
            continue
        try:
            texts.append(data.decode("utf-8"))
        except UnicodeError:
            continue
    return texts


def _ensure_answer(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    goal: str,
    sources: list[str],
) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text(
                "SELECT id::text FROM artifacts WHERE project_id = :project_id AND task_id = :task_id "
                "AND classification = 'task_answer' AND deleted_at IS NULL"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).scalar()
    if existing is not None:
        return str(existing)
    output = scripted_answer(source_text="\n".join(sources), goal=goal)
    if not output:
        return ""
    stored = store.put(owner_id=owner_id, project_id=project_id, data=output.encode(), media_type="text/plain", classification="task_answer")
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE artifacts SET task_id = :task_id WHERE project_id = :project_id AND id = :id AND task_id IS NULL"),
            {"task_id": task_id, "project_id": project_id, "id": stored["artifact_id"]},
        )
    _event(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        event_key=f"research-{task_id}",
        event_type="research.call",
        payload={"calls": 1, "model_mode": "scripted"},
    )
    return str(stored["artifact_id"])


def _answer_uses_source(data: bytes, goal: str, sources: list[str]) -> bool:
    try:
        answer = data.decode("utf-8").strip()
    except UnicodeError:
        return False
    if not answer or answer == goal.strip():
        return False
    for source in sources:
        for line in source.splitlines():
            fact = line.strip()
            if len(fact) >= 8 and fact not in goal and fact in answer:
                return True
    return False


def _artifact_ledger(database: Database, owner_id: str, project_id: str, artifact_id: str) -> tuple[str | None, bool]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT sha256, deleted_at FROM artifacts WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": artifact_id},
        ).mappings().first()
    if row is None:
        return None, True
    return str(row["sha256"]), row["deleted_at"] is not None


def _observe_bytes(store: ArtifactStore, owner_id: str, project_id: str, artifact_id: str) -> tuple[str, bytes | None]:
    try:
        data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
    except ArtifactError:
        return hashlib.sha256(b"unreadable").hexdigest(), None
    return hashlib.sha256(data).hexdigest(), data


def _task_status(database: Database, owner_id: str, project_id: str, task_id: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return str(conn.execute(text("SELECT status FROM tasks WHERE project_id = :project_id AND id = :id"), {"project_id": project_id, "id": task_id}).scalar_one())


def _verdict(endpoints: WorkerEndpoints, owner_id: str, project_id: str, verification_id: str) -> str:
    verifier = Database(endpoints.verifier_url)
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return str(conn.execute(text("SELECT verdict FROM verification_results WHERE id = :id"), {"id": verification_id}).scalar_one())


def _set_status(database: Database, *, owner_id: str, project_id: str, task_id: str, status: str, allowed: tuple[str, ...]) -> None:
    names = [f"allowed_{index}" for index in range(len(allowed))]
    clause = ", ".join(f":{name}" for name in names)
    params: dict[str, object] = {"status": status, "project_id": project_id, "id": task_id}
    params.update(dict(zip(names, allowed, strict=True)))
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE tasks SET status = :status, version = version + 1, projection_updated_at = now() "
                f"WHERE project_id = :project_id AND id = :id AND status IN ({clause})"
            ),
            params,
        )


def _event(database: Database, *, owner_id: str, project_id: str, task_id: str, event_key: str, event_type: str, payload: dict[str, Any]) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text("SELECT 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_key = :event_key"),
            {"project_id": project_id, "task_id": task_id, "event_key": event_key},
        ).first()
        if existing is not None:
            return
        sequence = conn.execute(
            text("SELECT coalesce(max(sequence), 0) + 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id"),
            {"project_id": project_id, "task_id": task_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO task_events (project_id, task_id, sequence, event_key, event_type, payload) "
                "VALUES (:project_id, :task_id, :sequence, :event_key, :event_type, CAST(:payload AS jsonb))"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "sequence": sequence,
                "event_key": event_key,
                "event_type": event_type,
                "payload": json.dumps(payload),
            },
        )
