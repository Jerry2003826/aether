import asyncio
import os
import uuid

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from aether_worker.activities import APPLIED, record_applied
from aether_worker.outbox import enqueue_task
from aether_worker.workflows import TaskWorkflow
from tests.integration.test_auth import _bootstrap


def test_task_and_command_commit_together(client, api_db) -> None:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "workflow-project"},
        json={"name": "工作流"},
    )
    assert project.status_code == 201, project.text
    created = enqueue_task(api_db, owner_id=session["owner_id"], project_id=project.json()["id"], goal="运行", command_key="start-1")
    from sqlalchemy import text

    with api_db.transaction(owner_id=session["owner_id"], project_id=project.json()["id"]) as conn:
        task = conn.execute(text("SELECT workflow_id FROM tasks WHERE id = :id"), {"id": created["task_id"]}).scalar_one()
        command = conn.execute(text("SELECT count(*) FROM command_outbox WHERE command_key = 'start-1'")).scalar_one()
    assert task == created["workflow_id"]
    assert command == 1
    with pytest.raises(Exception):
        enqueue_task(api_db, owner_id=session["owner_id"], project_id=project.json()["id"], goal="运行", command_key="start-1")
    with api_db.transaction(owner_id=session["owner_id"], project_id=project.json()["id"]) as conn:
        tasks = conn.execute(text("SELECT count(*) FROM tasks WHERE project_id = :project_id"), {"project_id": project.json()["id"]}).scalar_one()
    assert tasks == 1


def test_workflow_deduplicates_commands() -> None:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
            pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
        pytest.skip("AETHER_TEMPORAL_ADDRESS unset; workflow not_run")

    async def run() -> list[str]:
        APPLIED.clear()
        client = await Client.connect(address)
        task_id = str(uuid.uuid4())
        async with Worker(client, task_queue="aether-f07", workflows=[TaskWorkflow], activities=[record_applied]):
            handle = await client.start_workflow(TaskWorkflow.run, task_id, id=f"task-{task_id}", task_queue="aether-f07")
            await handle.signal(TaskWorkflow.command, {"command_id": "cmd-1"})
            await handle.signal(TaskWorkflow.command, {"command_id": "cmd-1"})
            await handle.signal(TaskWorkflow.close)
            result = await handle.result()
        return task_id, result

    task_id, applied = asyncio.run(run())
    assert applied == ["cmd-1"]
    assert APPLIED[task_id] == 1
