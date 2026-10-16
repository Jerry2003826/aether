"""P1-02: a deterministic activity failure converges instead of retrying forever."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_storage.artifacts import ArtifactError
from aether_storage.db import Database
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import apply_task_control, configure, execute_task, prepare_task
from tests.integration.system_audit import FACT, FACT_GOAL, answered_task, endpoints, key, project, store_for


def _temporal_address() -> str:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    return address


async def _run_until(address: str, queue: str, task_id: str, project_id: str, command_id: str, worker: Database, owner: str) -> tuple[int, str]:
    temporal = await Client.connect(address)
    async with Worker(temporal, task_queue=queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task, apply_task_control]):
        handle = await temporal.start_workflow(
            DurableTaskWorkflow.run,
            {"task_id": task_id, "intake_pause_seconds": 0},
            id=f"task-{task_id}",
            task_queue=queue,
        )
        await handle.signal(DurableTaskWorkflow.command, {"command_id": command_id, "project_id": project_id, "kind": "start"})
        attempt = 0
        deadline = time.monotonic() + 16
        while time.monotonic() < deadline:
            description = await handle.describe()
            pending = list(description.raw_description.pending_activities)
            if pending:
                attempt = max(attempt, int(pending[0].attempt))
            with worker.transaction(owner_id=owner, project_id=project_id) as conn:
                status = str(conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one())
            if status not in {"QUEUED", "RUNNING", "VERIFYING"}:
                await handle.terminate("audit cleanup")
                return attempt, status
            await asyncio.sleep(1)
        with worker.transaction(owner_id=owner, project_id=project_id) as conn:
            status = str(conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one())
        await handle.terminate("audit cleanup")
        return attempt, status


def test_poisoned_execute_activity_is_not_retried_forever(client, role_urls) -> None:
    """The tombstone that used to poison execute is a new logical artifact after P1-01.

    The activity must still finish inside the retry bound. A deterministic ArtifactError
    is covered by test_artifact_error_is_not_retried_forever.
    """
    address = _temporal_address()
    owner, project_id, headers, _answer, scripted = answered_task(client, role_urls, delete_answer=True)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": FACT_GOAL})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    configure(scripted)
    worker = Database(role_urls["aether_worker"])
    attempt, status = asyncio.run(
        _run_until(address, f"audit-poison-activity-{uuid.uuid4()}", task_id, project_id, created.json()["command_id"], worker, owner)
    )
    detail = f"16 秒内 execute_task 已重试到第 {attempt} 次，任务状态 {status}"
    assert status in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW"}, detail
    assert status != "RUNNING", detail
    assert attempt <= 3, detail


def test_artifact_error_is_not_retried_forever(client, role_urls, monkeypatch) -> None:
    address = _temporal_address()
    owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": FACT_GOAL})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]

    def _rejected(*_args, **_kwargs):
        raise ArtifactError("tombstone_retained", "已删除的产物仍占用内容地址")

    monkeypatch.setattr("aether_worker.goal_effect._ensure_answer", _rejected)
    configure(endpoints(role_urls, store_for(client, role_urls).root, provider="scripted"))
    worker = Database(role_urls["aether_worker"])
    attempt, status = asyncio.run(
        _run_until(address, f"audit-artifact-error-{uuid.uuid4()}", task_id, project_id, created.json()["command_id"], worker, owner)
    )
    detail = f"16 秒内 execute_task 已重试到第 {attempt} 次，任务状态 {status}"
    assert status in {"FAILED", "NEEDS_REVIEW"}, detail
    assert attempt <= 3, detail
