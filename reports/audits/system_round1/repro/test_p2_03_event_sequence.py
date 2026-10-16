"""P2-03 · 任务事件序号用 max(sequence)+1 分配，API 与 worker 同时写事件时撞唯一约束。

不变量 INV-PROJ-01：并发写入同一任务的事件不会让命令接收失败（HTTP 500）或活动失败。
"""

from __future__ import annotations

import json
import threading
import time

from sqlalchemy import text

from aether_api.control import _append_event
from aether_storage.db import Database
from audit_helpers import key, project


def test_api_command_event_and_worker_event_do_not_collide(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "并发事件"})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    api = Database(role_urls["aether_api"])
    errors: list[str] = []

    def api_side() -> None:
        try:
            with api.transaction(owner_id=owner, project_id=project_id) as conn:
                _append_event(conn, project_id=project_id, task_id=task_id, event_type="command.accepted", payload={"kind": "pause"})
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")

    # The same statements goal_effect._event runs, held open the way a slow worker transaction would be.
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        sequence = conn.execute(
            text("SELECT coalesce(max(sequence), 0) + 1 FROM task_events WHERE project_id = :p AND task_id = :t"),
            {"p": project_id, "t": task_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO task_events (project_id, task_id, sequence, event_key, event_type, payload) "
                "VALUES (:p, :t, :s, :k, 'task.running', CAST(:payload AS jsonb))"
            ),
            {"p": project_id, "t": task_id, "s": sequence, "k": f"running-{task_id}", "payload": json.dumps({"status": "RUNNING"})},
        )
        thread = threading.Thread(target=api_side)
        thread.start()
        time.sleep(0.8)
    thread.join(timeout=10)
    assert not errors, f"worker 事件提交后，同时进行的 API 命令事件失败：{errors}"
