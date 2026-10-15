"""Replay a workflow history recorded by the pre-patch task workflow."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.goal_effect import WorkerEndpoints, configure, execute_task, prepare_task
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.outbox import enqueue_task
from tests.integration.test_artifacts import _project

BASELINE = "e1cc9e5"
FACT = "事实：水在标准大气压下于一百摄氏度沸腾\n"


def _temporal_address() -> str:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if address:
        return address
    if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    pytest.skip("AETHER_TEMPORAL_ADDRESS unset; workflow not_run")


def _old_workflow(tmp_path: Path):
    source = subprocess.check_output(["git", "show", f"{BASELINE}:services/worker/aether_worker/durable_workflow.py"], text=True)
    path = tmp_path / "durable_workflow_before_patch.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("durable_workflow_before_patch", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(tmp_path))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DurableTaskWorkflow


def test_current_workflow_replays_history_from_before_the_patch(client, role_urls, tmp_path: Path) -> None:
    address = _temporal_address()
    owner, project, _headers = _project(client)
    worker_db = Database(role_urls["aether_worker"])
    root = client.app.state.settings.artifact_root
    ArtifactStore(root, worker_db).put(owner_id=owner, project_id=project, data=FACT.encode(), media_type="text/plain")
    def task():
        return enqueue_task(worker_db, owner_id=owner, project_id=project, goal="请根据已上传资料回答沸点", command_key=str(uuid.uuid4()))

    configure(
        WorkerEndpoints(
            worker_url=role_urls["aether_worker"],
            verifier_url=role_urls["aether_verifier"],
            dispatcher_url=role_urls["aether_dispatcher"],
            artifact_root=root,
            temporal_address=address,
            task_queue="replay-before-patch",
            research_provider="scripted",
        )
    )
    old_workflow = _old_workflow(tmp_path)
    queue = f"replay-before-{uuid.uuid4()}"

    async def record(pause: float):
        created = task()
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[old_workflow], activities=[prepare_task, execute_task]):
            handle = await temporal.start_workflow(
                old_workflow.run,
                {"task_id": created["task_id"], "intake_pause_seconds": pause},
                id=created["workflow_id"],
                task_queue=queue,
            )
            await handle.signal(old_workflow.command, {"command_id": created["command_key"], "project_id": project, "kind": "start"})
            await handle.signal(old_workflow.close)
            await handle.result()
            return await handle.fetch_history()

    paused = asyncio.run(record(1))
    immediate = asyncio.run(record(0))
    replayer = Replayer(workflows=[DurableTaskWorkflow])

    async def replay() -> None:
        await replayer.replay_workflow(paused)
        await replayer.replay_workflow(immediate)

    asyncio.run(replay())
