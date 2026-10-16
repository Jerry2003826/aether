"""Task creation and its start command commit in one database transaction."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database


def insert_start_command(conn, *, project_id: str, goal: str, command_key: str, owner_id: str, budget: dict | None = None, allowed_source_ids: list[str] | None = None) -> dict[str, str]:
    task_id = str(uuid.uuid4())
    workflow_id = f"task-{task_id}"
    payload = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "goal": goal,
        "command_id": command_key,
        "owner_id": owner_id,
        "budget": budget,
        "allowed_source_ids": allowed_source_ids,
    }
    digest = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    conn.execute(
        text("INSERT INTO tasks (id, project_id, original_goal, workflow_id, status) VALUES (:id, :project_id, :goal, :workflow_id, 'QUEUED')"),
        {"id": task_id, "project_id": project_id, "goal": goal, "workflow_id": workflow_id},
    )
    conn.execute(
        text(
            "INSERT INTO command_outbox (project_id, task_id, command_key, kind, payload, payload_sha256) "
            "VALUES (:project_id, :task_id, :command_key, 'start', CAST(:payload AS jsonb), :digest)"
        ),
        {"project_id": project_id, "task_id": task_id, "command_key": command_key, "payload": json.dumps(payload), "digest": digest},
    )
    return {"task_id": task_id, "workflow_id": workflow_id, "command_key": command_key}


def insert_followup_command(conn, *, project_id: str, task_id: str, workflow_id: str, command_key: str, kind: str, extra: dict | None = None) -> None:
    payload = {"task_id": task_id, "workflow_id": workflow_id, "command_id": command_key, "kind": kind}
    if extra:
        payload.update(extra)
    digest = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    conn.execute(
        text(
            "INSERT INTO command_outbox (project_id, task_id, command_key, kind, payload, payload_sha256) "
            "VALUES (:project_id, :task_id, :command_key, :kind, CAST(:payload AS jsonb), :digest)"
        ),
        {
            "project_id": project_id,
            "task_id": task_id,
            "command_key": command_key,
            "kind": kind,
            "payload": json.dumps(payload),
            "digest": digest,
        },
    )


def enqueue_task(database: Database, *, owner_id: str, project_id: str, goal: str, command_key: str, budget: dict | None = None, allowed_source_ids: list[str] | None = None) -> dict[str, str]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return insert_start_command(
            conn,
            project_id=project_id,
            goal=goal,
            command_key=command_key,
            owner_id=owner_id,
            budget=budget,
            allowed_source_ids=allowed_source_ids,
        )
