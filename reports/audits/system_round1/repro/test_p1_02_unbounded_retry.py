"""P1-02 · 确定性失败的活动被 Temporal 无限重试，任务永远停在 RUNNING。

不变量 INV-STAB-02：确定性失败必须收敛到终态（FAILED/NEEDS_REVIEW），不能无限重试。
触发条件借用 P1-01：被 tombstone 的答案字节让第二个任务的 store.put 每次都抛 ArtifactError。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_storage.db import Database
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import apply_task_control, configure, execute_task, prepare_task
from audit_helpers import FACT_GOAL, answered_task, key


def test_poisoned_execute_activity_is_not_retried_forever(client, role_urls) -> None:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    owner, project_id, headers, _answer, scripted = answered_task(client, role_urls, delete_answer=True)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": FACT_GOAL})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    configure(scripted)
    queue = f"audit-poison-activity-{uuid.uuid4()}"
    worker = Database(role_urls["aether_worker"])

    async def scenario() -> tuple[int, str]:
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task, apply_task_control]):
            handle = await temporal.start_workflow(
                DurableTaskWorkflow.run,
                {"task_id": task_id, "intake_pause_seconds": 0},
                id=f"task-{task_id}",
                task_queue=queue,
            )
            await handle.signal(DurableTaskWorkflow.command, {"command_id": created.json()["command_id"], "project_id": project_id, "kind": "start"})
            attempt = 0
            deadline = time.monotonic() + 16
            while time.monotonic() < deadline:
                description = await handle.describe()
                pending = list(description.raw_description.pending_activities)
                if pending:
                    attempt = max(attempt, int(pending[0].attempt))
                await asyncio.sleep(1)
            with worker.transaction(owner_id=owner, project_id=project_id) as conn:
                status = str(conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one())
            await handle.terminate("audit cleanup")
            return attempt, status

    attempt, status = asyncio.run(scenario())
    detail = f"16 秒内 execute_task 已重试到第 {attempt} 次，任务状态 {status}"
    assert status in {"FAILED", "NEEDS_REVIEW"}, detail
    assert attempt <= 3, detail
