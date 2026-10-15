import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_domain.security import sha256_text
from aether_storage.db import Database
from aether_worker.activities import APPLIED, record_applied
from aether_worker.dispatcher import deliver_pending
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


class _UnavailableTemporal:
    async def start_workflow(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("temporal unavailable")


def test_dispatcher_does_not_confirm_when_temporal_is_unreachable(client, api_db, role_urls) -> None:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "dispatch-down"},
        json={"name": "投递失败"},
    )
    assert project.status_code == 201, project.text
    created = enqueue_task(api_db, owner_id=session["owner_id"], project_id=project.json()["id"], goal="投递", command_key="start-down")
    dispatcher = Database(role_urls["aether_dispatcher"])
    result = asyncio.run(deliver_pending(dispatcher, _UnavailableTemporal(), task_queue="aether-down"))  # type: ignore[arg-type]
    assert result == {"delivered": [], "failed": ["start-down"]}
    with api_db.transaction(owner_id=session["owner_id"], project_id=project.json()["id"]) as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": created["task_id"]}).scalar_one()
        command = conn.execute(text("SELECT status FROM command_outbox WHERE command_key = 'start-down'")).scalar_one()
    assert status == "QUEUED"
    assert command == "DELIVERING"


def test_dispatcher_delivers_outbox_to_temporal_without_marking_success(client, api_db, role_urls) -> None:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
            pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
        pytest.skip("AETHER_TEMPORAL_ADDRESS unset; workflow not_run")
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "dispatch-live"},
        json={"name": "投递"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    created = enqueue_task(api_db, owner_id=session["owner_id"], project_id=project_id, goal="投递", command_key="start-live")
    duplicate = {"task_id": created["task_id"], "workflow_id": created["workflow_id"], "goal": "投递", "command_id": "start-live"}
    digest = sha256_text(json.dumps(duplicate, sort_keys=True, separators=(",", ":")))
    with api_db.transaction(owner_id=session["owner_id"], project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO command_outbox (project_id, task_id, command_key, kind, payload, payload_sha256) "
                "VALUES (:project_id, :task_id, 'start-live-replay', 'start', CAST(:payload AS jsonb), :digest)"
            ),
            {"project_id": project_id, "task_id": created["task_id"], "payload": json.dumps(duplicate), "digest": digest},
        )
    dispatcher = Database(role_urls["aether_dispatcher"])
    queue = f"aether-dispatch-{uuid.uuid4()}"

    async def run() -> list[str]:
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[TaskWorkflow], activities=[record_applied]):
            first = await deliver_pending(dispatcher, temporal, task_queue=queue)
            assert first["delivered"] == ["start-live", "start-live-replay"]
            assert first["failed"] == []
            handle = temporal.get_workflow_handle(created["workflow_id"])
            applied: list[str] = []
            for _ in range(20):
                applied = list(await handle.query(TaskWorkflow.applied))
                if applied == ["start-live"]:
                    break
                await asyncio.sleep(0.1)
            second = await deliver_pending(dispatcher, temporal, task_queue=queue)
            assert second == {"delivered": [], "failed": []}
            return applied

    applied = asyncio.run(run())
    assert applied == ["start-live"]
    with api_db.transaction(owner_id=session["owner_id"], project_id=project_id) as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": created["task_id"]}).scalar_one()
        rows = conn.execute(text("SELECT status FROM command_outbox WHERE project_id = :project_id ORDER BY command_key"), {"project_id": project_id}).scalars().all()
    assert status == "QUEUED"
    assert rows == ["DELIVERED", "DELIVERED"]
