"""P1-07 · 当前工作流不能回放 4ba3b2f 时期收到过控制命令的历史。

不变量 INV-STAB-03：工作流改版必须能回放旧历史（docs/04 §4）。任务工作流从不 close，
旧版本启动的工作流在升级后仍然在跑；控制分支改成 apply_task_control 时没有 workflow.patched 保护。
现有 test_workflow_replay 只回放了只含 start 的历史。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import configure, execute_task, prepare_task
from aether_worker.outbox import enqueue_task, insert_followup_command
from audit_helpers import endpoints, project

ROOT = Path(__file__).resolve().parents[4]
OLD = ROOT / "tests" / "fixtures" / "durable_workflow_4ba3b2f.py"


def _old_workflow(tmp_path: Path):
    path = tmp_path / "durable_workflow_4ba3b2f_audit.py"
    path.write_text(OLD.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("durable_workflow_4ba3b2f_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(tmp_path))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DurableTaskWorkflow


def test_current_workflow_replays_an_old_history_that_saw_a_pause(client, role_urls, tmp_path) -> None:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    owner, project_id, _headers = project(client)
    worker_db = Database(role_urls["aether_worker"])
    root = client.app.state.settings.artifact_root
    ArtifactStore(root, worker_db).put(owner_id=owner, project_id=project_id, data="事实：水在标准大气压下于一百摄氏度沸腾\n".encode(), media_type="text/plain")
    created = enqueue_task(worker_db, owner_id=owner, project_id=project_id, goal="请根据已上传资料回答沸点", command_key=str(uuid.uuid4()))
    pause_key = str(uuid.uuid4())
    with worker_db.transaction(owner_id=owner, project_id=project_id) as conn:
        insert_followup_command(conn, project_id=project_id, task_id=created["task_id"], workflow_id=created["workflow_id"], command_key=pause_key, kind="pause")
    configure(endpoints(role_urls, root, provider="scripted"))
    old_workflow = _old_workflow(tmp_path)
    queue = f"audit-replay-{uuid.uuid4()}"

    async def record():
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[old_workflow], activities=[prepare_task, execute_task]):
            handle = await temporal.start_workflow(old_workflow.run, {"task_id": created["task_id"], "intake_pause_seconds": 0}, id=created["workflow_id"], task_queue=queue)
            await handle.signal(old_workflow.command, {"command_id": created["command_key"], "project_id": project_id, "kind": "start"})
            await handle.signal(old_workflow.command, {"command_id": pause_key, "project_id": project_id, "kind": "pause"})
            await handle.signal(old_workflow.close)
            await handle.result()
            return await handle.fetch_history()

    history = asyncio.run(record())
    error = None
    try:
        asyncio.run(Replayer(workflows=[DurableTaskWorkflow]).replay_workflow(history))
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    assert error is None, f"旧历史（start + pause）用当前工作流回放失败：{error}"
