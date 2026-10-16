"""Insert the owner's approve_goal command. The worker does not invent this row."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database


def record_owner_approval(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    workflow_id: str | None = None,
) -> str:
    command_key = str(uuid.uuid4())
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        if workflow_id is None:
            workflow_id = str(
                conn.execute(
                    text("SELECT workflow_id FROM tasks WHERE project_id = :project_id AND id = :id"),
                    {"project_id": project_id, "id": task_id},
                ).scalar_one()
            )
        payload = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "command_id": command_key,
            "kind": "approve_goal",
            "owner_id": owner_id,
        }
        digest = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        conn.execute(
            text(
                "INSERT INTO command_outbox (project_id, task_id, command_key, kind, payload, payload_sha256) "
                "VALUES (:project_id, :task_id, :command_key, 'approve_goal', CAST(:payload AS jsonb), :digest)"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "command_key": command_key,
                "payload": json.dumps(payload),
                "digest": digest,
            },
        )
    return command_key
