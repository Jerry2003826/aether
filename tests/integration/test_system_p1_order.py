"""P1-03: one task's commands stay in order, and an accepted cancel is not cleared by resume."""

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
from aether_worker.dispatcher import deliver_pending
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import apply_task_control, configure, execute_task, prepare_task
from tests.integration.system_audit import endpoints, fake_transport, key, project, store_for

FACT = "接口请求上限为每分钟六十次。"
GOAL = "根据资料，接口每分钟最多允许多少次请求？只回答请求限额。"


class _Handle:
    def __init__(self, recorder: "_Recorder") -> None:
        self.recorder = recorder

    async def signal(self, _signal, payload) -> None:
        self.recorder.signal(payload)


class _Recorder:
    def __init__(self, fail_kind: str) -> None:
        self.fail_kind = fail_kind
        self.failed_once = False
        self.order: list[str] = []

    def signal(self, payload) -> None:
        if payload["kind"] == self.fail_kind and not self.failed_once:
            self.failed_once = True
            raise ConnectionError("transient temporal error")
        self.order.append(payload["kind"])

    async def start_workflow(self, *args, **kwargs):
        return _Handle(self)


def _command(client, project_id: str, task_id: str, headers: dict[str, str], kind: str) -> str:
    sent = client.post(
        f"/v1/projects/{project_id}/tasks/{task_id}/commands",
        headers={**headers, "Idempotency-Key": key()},
        json={"command_id": str(uuid.uuid4()), "kind": kind},
    )
    assert sent.status_code == 202, sent.text
    return sent.json()["command_id"]


def test_dispatcher_keeps_per_task_order_when_one_signal_fails(client, role_urls) -> None:
    _owner, project_id, headers = project(client)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": GOAL})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    _command(client, project_id, task_id, headers, "pause")
    _command(client, project_id, task_id, headers, "resume")
    recorder = _Recorder("pause")
    dispatcher = Database(role_urls["aether_dispatcher"])
    asyncio.run(deliver_pending(dispatcher, recorder, task_queue="audit-order"))  # type: ignore[arg-type]
    asyncio.run(deliver_pending(dispatcher, recorder, task_queue="audit-order"))  # type: ignore[arg-type]
    assert recorder.order == ["start", "pause", "resume"], f"实际送达顺序 {recorder.order}，用户发出的顺序是 start, pause, resume"


class _FlakyHandle:
    def __init__(self, inner, flaky: "_FlakyClient") -> None:
        self.inner = inner
        self.flaky = flaky

    async def signal(self, signal, payload) -> None:
        if payload["kind"] == self.flaky.fail_kind and not self.flaky.failed_once:
            self.flaky.failed_once = True
            raise ConnectionError("transient temporal error")
        self.flaky.order.append(payload["kind"])
        await self.inner.signal(signal, payload)


class _FlakyClient:
    def __init__(self, inner: Client, fail_kind: str | None) -> None:
        self.inner = inner
        self.fail_kind = fail_kind
        self.failed_once = fail_kind is None
        self.order: list[str] = []

    async def start_workflow(self, *args, **kwargs):
        return await self.inner.start_workflow(*args, **kwargs)

    def get_workflow_handle(self, workflow_id: str):
        return _FlakyHandle(self.inner.get_workflow_handle(workflow_id), self)


def test_cancel_then_resume_never_runs_the_model_when_a_signal_is_retried(client, role_urls, monkeypatch) -> None:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    state = fake_transport(monkeypatch, output=f"资料记载：{FACT}")
    configure(endpoints(role_urls, store_for(client, role_urls).root))
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": GOAL})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    _command(client, project_id, task_id, headers, "pause")
    worker = Database(role_urls["aether_worker"])
    dispatcher = Database(role_urls["aether_dispatcher"])
    queue = f"audit-cancel-order-{uuid.uuid4()}"

    def status() -> str:
        with worker.transaction(owner_id=owner, project_id=project_id) as conn:
            return str(conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one())

    async def wait_for(predicate, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.2)

    async def scenario() -> list[str]:
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task, apply_task_control]):
            await deliver_pending(dispatcher, _FlakyClient(temporal, None), task_queue=queue, workflow=DurableTaskWorkflow, intake_pause_seconds=30)  # type: ignore[arg-type]
            await wait_for(lambda: status() == "PAUSED")
            assert status() == "PAUSED"
            _command(client, project_id, task_id, headers, "cancel")
            _command(client, project_id, task_id, headers, "resume")
            flaky = _FlakyClient(temporal, "cancel")
            await deliver_pending(dispatcher, flaky, task_queue=queue, workflow=DurableTaskWorkflow, intake_pause_seconds=30)  # type: ignore[arg-type]
            await wait_for(lambda: status() in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW"} or state["calls"] > 0, timeout=10)
            await deliver_pending(dispatcher, flaky, task_queue=queue, workflow=DurableTaskWorkflow, intake_pause_seconds=30)  # type: ignore[arg-type]
            await wait_for(lambda: status() in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW", "CANCELLED"}, timeout=10)
            await asyncio.sleep(1)
            return flaky.order

    order = asyncio.run(scenario())
    final = status()
    detail = f"送达顺序={order} 最终状态={final} 取消之后的模型调用次数={state['calls']}"
    assert state["calls"] == 0, detail
    assert final == "CANCELLED", detail
