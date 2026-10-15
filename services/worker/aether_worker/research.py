"""Scripted research step and independent verification. The model call is not live."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import text
from temporalio import activity

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database


EFFECTS: dict[str, Any] = {}


@activity.defn
async def record_research_effect(payload: dict) -> int:
    return record_command_effect(
        EFFECTS["database"],
        owner_id=str(EFFECTS["owner_id"]),
        project_id=str(EFFECTS["project_id"]),
        task_id=str(payload["task_id"]),
        command_id=str(payload["command_id"]),
    )


def read_local_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("local_text_rejected") from exc


def scripted_research(*, store: ArtifactStore, gateway: Any, token: str, owner_id: str, project_id: str, artifact_id: str) -> dict[str, Any]:
    data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
    digest = hashlib.sha256(data).hexdigest()
    extracted = read_local_text(data)
    model = gateway.complete(token, {"alias": "actor", "prompt": extracted})
    return {
        "artifact_sha256": digest,
        "tool_text": extracted,
        "model_output": model.get("output"),
        "model_mode": "scripted",
        "usage_status": model.get("usage_status"),
        "gateway_calls": 1,
    }


def record_command_effect(database: Database, *, owner_id: str, project_id: str, task_id: str, command_id: str) -> int:
    event_key = f"research-{command_id}"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text("SELECT 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_key = :event_key"),
            {"project_id": project_id, "task_id": task_id, "event_key": event_key},
        ).first()
        if existing is None:
            sequence = conn.execute(
                text("SELECT coalesce(max(sequence), 0) + 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id"),
                {"project_id": project_id, "task_id": task_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO task_events (project_id, task_id, sequence, event_key, event_type, payload) "
                    "VALUES (:project_id, :task_id, :sequence, :event_key, 'research.call', CAST(:payload AS jsonb))"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "sequence": sequence,
                    "event_key": event_key,
                    "payload": '{"calls": 1}',
                },
            )
        count = conn.execute(
            text("SELECT count(*) FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_type = 'research.call'"),
            {"project_id": project_id, "task_id": task_id},
        ).scalar_one()
    return int(count)


def record_verification(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    contract_id: str,
    artifact_id: str,
    report_artifact_id: str,
    criterion_id: str,
    artifact_sha256: str,
    observed_sha256: str,
    contract_sha256: str,
    expires_at: datetime,
    now: datetime,
    actor_verdict: str | None = None,
) -> dict[str, str]:
    if expires_at <= now or artifact_sha256 != observed_sha256:
        verdict = "FAIL"
        reason = "候选已过期或哈希不一致"
    else:
        verdict = "PASS"
        reason = "哈希一致且候选未过期"
    if actor_verdict is not None and actor_verdict != verdict:
        reason = f"{reason}；已忽略 actor 给出的 {actor_verdict}"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        identity = conn.execute(text("SELECT current_user")).scalar_one()
        verification_id = conn.execute(
            text(
                "INSERT INTO verification_results (project_id, task_id, contract_id, artifact_id, criterion_id, verdict, "
                "artifact_sha256, contract_sha256, verifier_identity, report_artifact_id, reason, checked_at) "
                "VALUES (:project_id, :task_id, :contract_id, :artifact_id, :criterion_id, :verdict, :artifact_sha256, "
                ":contract_sha256, :identity, :report_artifact_id, :reason, now()) RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "contract_id": contract_id,
                "artifact_id": artifact_id,
                "criterion_id": criterion_id,
                "verdict": verdict,
                "artifact_sha256": artifact_sha256,
                "contract_sha256": contract_sha256,
                "identity": identity,
                "report_artifact_id": report_artifact_id,
                "reason": reason,
            },
        ).scalar_one()
    return {"id": str(verification_id), "verdict": verdict, "reason": reason, "verifier": str(identity)}
