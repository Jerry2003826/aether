"""Deliver outbox commands to Temporal. This process is not workflow code."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from aether_storage.db import Database
from aether_worker.workflows import TaskWorkflow

# Task workflow commands only. Console, memory, and verifier rows stay on their own appliers.
TEMPORAL_COMMAND_KINDS = ("start", "approve_goal", "pause", "resume", "cancel")


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
        return {}
    if isinstance(value, dict):
        return value
    return {}


def _claim(database: Database, limit: int) -> list[dict[str, Any]]:
    kind_list = ", ".join(f"'{kind}'" for kind in TEMPORAL_COMMAND_KINDS)
    with database.transaction() as conn:
        rows = conn.execute(
            text(
                f"""
                WITH claimed AS (
                  SELECT project_id, id
                  FROM command_outbox
                  WHERE status IN ('PENDING', 'DELIVERING')
                    AND kind IN ({kind_list})
                  ORDER BY created_at, command_key
                  FOR UPDATE SKIP LOCKED
                  LIMIT :limit
                ),
                updated AS (
                  UPDATE command_outbox AS outbox
                  SET status = 'DELIVERING', attempts = attempts + 1
                  FROM claimed
                  WHERE outbox.project_id = claimed.project_id AND outbox.id = claimed.id
                  RETURNING outbox.project_id::text AS project_id, outbox.id::text AS id,
                            outbox.task_id::text AS task_id, outbox.command_key, outbox.kind,
                            outbox.payload, outbox.created_at
                )
                SELECT project_id, id, task_id, command_key, kind, payload, created_at
                FROM updated
                ORDER BY created_at, command_key
                """
            ),
            {"limit": limit},
        ).mappings().all()
    return [dict(row) for row in rows]


def _mark(database: Database, project_id: str, command_id: str, status: str) -> None:
    # The project-scoped policy still applies to the dispatcher. Set the same project as the row.
    with database.transaction(project_id=project_id) as conn:
        conn.execute(
            text(
                """
                UPDATE command_outbox
                SET status = :status,
                    delivered_at = CASE WHEN :status = 'DELIVERED' THEN now() ELSE delivered_at END
                WHERE project_id = :project_id AND id = :id AND status = 'DELIVERING'
                """
            ),
            {"status": status, "project_id": project_id, "id": command_id},
        )


async def _signal(client: Client, *, workflow: type[TaskWorkflow], workflow_id: str, task_id: str, command_id: str, project_id: str, task_queue: str, intake_pause_seconds: float = 0, kind: str = "start") -> None:
    argument: str | dict[str, object] = task_id
    if intake_pause_seconds > 0 and getattr(workflow, "accepts_intake_pause", False):
        argument = {"task_id": task_id, "intake_pause_seconds": intake_pause_seconds}
    try:
        handle = await client.start_workflow(workflow.run, argument, id=workflow_id, task_queue=task_queue)
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    await handle.signal(workflow.command, {"command_id": command_id, "project_id": project_id, "kind": kind})


def _reset_pending(database: Database, project_id: str, command_id: str) -> None:
    with database.transaction(project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE command_outbox SET status = 'PENDING' "
                "WHERE project_id = :project_id AND id = :id AND status = 'DELIVERING'"
            ),
            {"project_id": project_id, "id": command_id},
        )


async def deliver_pending(database: Database, client: Client, *, task_queue: str, limit: int = 20, workflow: type[TaskWorkflow] = TaskWorkflow, intake_pause_seconds: float = 0) -> dict[str, list[str]]:
    """Signal pending commands in per-task order. A failed signal blocks later commands of that task."""
    delivered: list[str] = []
    failed: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _claim(database, limit):
        grouped.setdefault(str(row["task_id"] or ""), []).append(row)
    for commands in grouped.values():
        for index, row in enumerate(commands):
            payload = _payload(row["payload"])
            command_key = str(row["command_key"])
            task_id = row["task_id"]
            project_id = str(row["project_id"])
            if not task_id:
                _mark(database, project_id, str(row["id"]), "FAILED")
                failed.append(command_key)
                continue
            workflow_id = str(payload.get("workflow_id") or f"task-{task_id}")
            command_id = str(payload.get("command_id") or command_key)
            try:
                await _signal(
                    client,
                    workflow=workflow,
                    workflow_id=workflow_id,
                    task_id=str(task_id),
                    command_id=command_id,
                    project_id=project_id,
                    task_queue=task_queue,
                    intake_pause_seconds=intake_pause_seconds,
                    kind=str(row["kind"]),
                )
            except Exception:
                failed.append(command_key)
                for later in commands[index + 1 :]:
                    _reset_pending(database, str(later["project_id"]), str(later["id"]))
                break
            _mark(database, project_id, str(row["id"]), "DELIVERED")
            delivered.append(command_key)
    return {"delivered": delivered, "failed": failed}
