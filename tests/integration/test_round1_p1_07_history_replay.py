"""System audit round 1, P1-07: the current task workflow replays histories from every released version.

INV-STAB-03: a workflow change must replay old histories (docs/04 §4). Task workflows never close, so
workflows started by an older release are still running after an upgrade. Each released version has a
history with start / pause / resume / cancel, and the current workflow must replay all of them.

The first release is recorded live from ``tests/fixtures/durable_workflow_4ba3b2f.py``. Later releases are
histories in ``tests/fixtures/workflow_histories/``, recorded once by running that release's
``git show <rev>:services/worker/aether_worker/durable_workflow.py`` against the real activities with the same
signals: start, wait for execute_task, then pause, resume, cancel and close.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowHandle, WorkflowHistory
from temporalio.worker import Replayer, Worker

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import WorkerEndpoints, apply_task_control, configure, execute_task, prepare_task
from aether_worker.outbox import enqueue_task, insert_followup_command
from tests.integration.test_auth import ORIGIN, _bootstrap

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HISTORIES = FIXTURES / "workflow_histories"
FACT = "事实：水在标准大气压下于一百摄氏度沸腾\n"
# The workflow module shipped by each later merge to main, with the patch marker its history must carry:
# e1cc9e5 (PR #7) = 75ec05b, 967b55f (PR #11) = 0359da5, d6ed382 (PR #12) = 48281f3, 80f1d39 (PR #14).
LATER_RELEASES = {
    "75ec05b": {"intake-pause-interruptible"},
    "0359da5": {"execute-interruptible"},
    "48281f3": {"execute-interruptible"},
    "80f1d39": {"bounded-activity-retry", "execute-interruptible"},
}


def _address() -> str:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    return address


def _released_workflow(tmp_path: Path, revision: str):
    path = tmp_path / f"durable_workflow_{revision}_round1.py"
    path.write_text((FIXTURES / f"durable_workflow_{revision}.py").read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(tmp_path))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DurableTaskWorkflow


class _Project:
    def __init__(self, client, role_urls) -> None:
        _bootstrap(client)
        session = client.get("/v1/auth/session").json()
        headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        created = client.post("/v1/projects", headers={**headers, "Idempotency-Key": f"round1-{uuid.uuid4().hex}"}, json={"name": "回放"})
        assert created.status_code == 201, created.text
        self.owner = session["owner_id"]
        self.project_id = created.json()["id"]
        self.worker = Database(role_urls["aether_worker"])
        root = client.app.state.settings.artifact_root
        ArtifactStore(root, self.worker).put(owner_id=self.owner, project_id=self.project_id, data=FACT.encode(), media_type="text/plain")
        configure(
            WorkerEndpoints(
                worker_url=role_urls["aether_worker"],
                verifier_url=role_urls["aether_verifier"],
                dispatcher_url=role_urls["aether_dispatcher"],
                artifact_root=root,
                temporal_address="127.0.0.1:9",
                task_queue=f"round1-{uuid.uuid4().hex}",
                research_provider="scripted",
            )
        )

    def task(self) -> dict[str, str]:
        return enqueue_task(self.worker, owner_id=self.owner, project_id=self.project_id, goal="请根据已上传资料回答沸点", command_key=str(uuid.uuid4()))

    def control(self, created: dict[str, str], kind: str) -> dict[str, str]:
        command_key = str(uuid.uuid4())
        with self.worker.transaction(owner_id=self.owner, project_id=self.project_id) as conn:
            insert_followup_command(conn, project_id=self.project_id, task_id=created["task_id"], workflow_id=created["workflow_id"], command_key=command_key, kind=kind)
        return {"command_id": command_key, "project_id": self.project_id, "kind": kind}


def _scheduled(history, name: str) -> int:
    return sum(
        1
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes") and event.activity_task_scheduled_event_attributes.activity_type.name == name
    )


async def _until_executed(handle: WorkflowHandle) -> None:
    """Wait until the start command's execute activity has completed."""
    for _ in range(400):
        history = await handle.fetch_history()
        scheduled = {
            event.event_id
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes") and event.activity_task_scheduled_event_attributes.activity_type.name == "execute_task"
        }
        if any(
            event.HasField("activity_task_completed_event_attributes") and event.activity_task_completed_event_attributes.scheduled_event_id in scheduled
            for event in history.events
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("execute_task never completed")


def test_current_workflow_replays_an_old_history_that_saw_a_pause(client, role_urls, tmp_path) -> None:
    address = _address()
    project = _Project(client, role_urls)
    created = project.task()
    pause = project.control(created, "pause")
    old_workflow = _released_workflow(tmp_path, "4ba3b2f")
    queue = f"round1-replay-{uuid.uuid4()}"

    async def record():
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[old_workflow], activities=[prepare_task, execute_task]):
            handle = await temporal.start_workflow(old_workflow.run, {"task_id": created["task_id"], "intake_pause_seconds": 0}, id=created["workflow_id"], task_queue=queue)
            await handle.signal(old_workflow.command, {"command_id": created["command_key"], "project_id": project.project_id, "kind": "start"})
            await handle.signal(old_workflow.command, pause)
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


def test_current_workflow_replays_start_pause_resume_cancel_from_the_first_release(client, role_urls, tmp_path) -> None:
    address = _address()
    project = _Project(client, role_urls)
    created = project.task()
    controls = [project.control(created, kind) for kind in ("pause", "resume", "cancel")]
    old_workflow = _released_workflow(tmp_path, "4ba3b2f")
    queue = f"round1-replay-{uuid.uuid4()}"

    async def record():
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[old_workflow], activities=[prepare_task, execute_task]):
            handle = await temporal.start_workflow(old_workflow.run, {"task_id": created["task_id"], "intake_pause_seconds": 0}, id=created["workflow_id"], task_queue=queue)
            await handle.signal(old_workflow.command, {"command_id": created["command_key"], "project_id": project.project_id, "kind": "start"})
            await _until_executed(handle)
            for control in controls:
                await handle.signal(old_workflow.command, control)
            await handle.signal(old_workflow.close)
            await handle.result()
            return await handle.fetch_history()

    history = asyncio.run(record())
    assert _scheduled(history, "prepare_task") == 4
    error = None
    try:
        asyncio.run(Replayer(workflows=[DurableTaskWorkflow]).replay_workflow(history))
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    assert error is None, f"4ba3b2f 的历史（start / pause / resume / cancel）用当前工作流回放失败：{error}"


def _patches(history: WorkflowHistory) -> set[str]:
    found = set()
    for event in history.events:
        if event.HasField("marker_recorded_event_attributes"):
            for payload in event.marker_recorded_event_attributes.details["patch-data"].payloads:
                found.add(json.loads(payload.data)["id"])
    return found


@pytest.mark.parametrize("revision", sorted(LATER_RELEASES))
def test_current_workflow_replays_recorded_histories_from_later_releases(revision: str) -> None:
    """75ec05b leaves no marker unless the intake pause is positive, so its history was recorded with 0.2 s.

    ADR-F27 (P1-07) lists the marker-free histories that stay ambiguous.
    """
    recorded = json.loads((HISTORIES / f"durable_workflow_{revision}.json").read_text(encoding="utf-8"))
    history = WorkflowHistory.from_json(f"recorded-{revision}", recorded)
    assert _scheduled(history, "apply_task_control") == 3
    assert _patches(history) == LATER_RELEASES[revision]
    error = None
    try:
        asyncio.run(Replayer(workflows=[DurableTaskWorkflow]).replay_workflow(history))
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    assert error is None, f"{revision} 的历史（start / pause / resume / cancel）用当前工作流回放失败：{error}"


def test_current_workflow_replays_a_pause_queued_with_the_start_under_bounded_retry() -> None:
    """80f1d39 drained a pause queued with the start right after prepare; only bounded-activity-retry marks that release."""
    recorded = json.loads((HISTORIES / "durable_workflow_80f1d39_pause_at_intake.json").read_text(encoding="utf-8"))
    history = WorkflowHistory.from_json("recorded-80f1d39-pause-at-intake", recorded)
    assert _scheduled(history, "prepare_task") == 1
    assert _scheduled(history, "apply_task_control") == 1
    assert _scheduled(history, "execute_task") == 0
    assert _patches(history) == {"bounded-activity-retry"}
    error = None
    try:
        asyncio.run(Replayer(workflows=[DurableTaskWorkflow]).replay_workflow(history))
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    assert error is None, f"80f1d39 的历史（start 与 pause 同时排队）用当前工作流回放失败：{error}"


def test_a_workflow_started_by_the_first_release_applies_new_controls_after_the_upgrade(client, role_urls, tmp_path) -> None:
    """Old history replays the old way; a pause that arrives after the upgrade goes through apply_task_control."""
    address = _address()
    project = _Project(client, role_urls)
    created = project.task()
    pause = project.control(created, "pause")
    old_workflow = _released_workflow(tmp_path, "4ba3b2f")
    queue = f"round1-upgrade-{uuid.uuid4()}"

    async def run():
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[old_workflow], activities=[prepare_task, execute_task]):
            handle = await temporal.start_workflow(old_workflow.run, {"task_id": created["task_id"], "intake_pause_seconds": 0}, id=created["workflow_id"], task_queue=queue)
            await handle.signal(old_workflow.command, {"command_id": created["command_key"], "project_id": project.project_id, "kind": "start"})
            await _until_executed(handle)
        async with Worker(
            temporal,
            task_queue=queue,
            workflows=[DurableTaskWorkflow],
            activities=[prepare_task, execute_task, apply_task_control],
            max_cached_workflows=0,
        ):
            handle = temporal.get_workflow_handle(created["workflow_id"])
            await handle.signal(DurableTaskWorkflow.command, pause)
            await handle.signal(DurableTaskWorkflow.close)
            await handle.result()
            return await handle.fetch_history()

    history = asyncio.run(run())
    assert _scheduled(history, "prepare_task") == 1
    assert _scheduled(history, "execute_task") == 1
    assert _scheduled(history, "apply_task_control") == 1
    asyncio.run(Replayer(workflows=[DurableTaskWorkflow]).replay_workflow(history))
