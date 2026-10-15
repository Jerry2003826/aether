"""Turn an accepted task start into a durable artifact and an independent verification.

The activity is idempotent. A worker restart or a Temporal retry must not add a second
artifact, contract, or verification row for the same task.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from temporalio import activity

from aether_domain.security import sha256_text
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.research import record_verification

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


def report_document(digest: str) -> bytes:
    return json.dumps({"artifact_sha256": digest, "criterion_id": CRITERION_ID}, sort_keys=True, separators=(",", ":")).encode()


@activity.defn
async def materialize_goal(payload: dict) -> dict[str, str]:
    if ENDPOINTS is None:
        raise RuntimeError("worker endpoints are not configured")
    return materialize_goal_effect(ENDPOINTS, payload)


def materialize_goal_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    project_id = str(payload.get("project_id") or "")
    task_id = str(payload.get("task_id") or "")
    command_id = str(payload.get("command_id") or "")
    if not project_id or not task_id or not command_id:
        return {"status": "ignored"}
    worker = Database(endpoints.worker_url)
    command = _command(worker, project_id, command_id)
    if command is None or str(command["kind"]) != "start":
        return {"status": "ignored"}
    body = command["payload"] if isinstance(command["payload"], dict) else json.loads(command["payload"])
    owner_id = str(body.get("owner_id") or "")
    goal = str(body.get("goal") or "")
    if not owner_id or not goal:
        raise RuntimeError("start command is missing owner_id or goal")
    store = ArtifactStore(endpoints.artifact_root, worker)
    contract_id, contract_sha = _ensure_contract(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal)
    goal_bytes = goal_document(task_id, goal)
    goal_row = _ensure_artifact(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        classification="task_goal",
        data=goal_bytes,
        media_type="text/plain",
    )
    report_row = _ensure_artifact(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        classification="task_goal_report",
        data=report_document(goal_row["sha256"]),
        media_type="text/plain",
    )
    verification_id = _ensure_verification(
        endpoints,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        contract_id=contract_id,
        contract_sha=contract_sha,
        artifact_id=goal_row["id"],
        artifact_sha=goal_row["sha256"],
        report_artifact_id=report_row["id"],
    )
    return {
        "status": "materialized",
        "artifact_id": goal_row["id"],
        "report_artifact_id": report_row["id"],
        "verification_id": verification_id,
    }


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
    artifact_sha: str,
    report_artifact_id: str,
) -> str:
    verifier = Database(endpoints.verifier_url)
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text(
                "SELECT id::text FROM verification_results "
                "WHERE project_id = :project_id AND task_id = :task_id AND criterion_id = :criterion_id "
                "ORDER BY created_at LIMIT 1"
            ),
            {"project_id": project_id, "task_id": task_id, "criterion_id": CRITERION_ID},
        ).scalar()
    if existing is not None:
        return str(existing)
    now = datetime.now(UTC)
    recorded = record_verification(
        verifier,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        contract_id=contract_id,
        artifact_id=artifact_id,
        report_artifact_id=report_artifact_id,
        criterion_id=CRITERION_ID,
        artifact_sha256=artifact_sha,
        observed_sha256=artifact_sha,
        contract_sha256=contract_sha,
        expires_at=now + timedelta(hours=24),
        now=now,
    )
    return str(recorded["id"])
