"""Round 4 regressions on real PostgreSQL. Only the provider transport is replaced."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
import uuid
from pathlib import Path

import psycopg
from types import SimpleNamespace

import litellm
import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_storage.artifacts import ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import (
    WorkerEndpoints,
    apply_task_control,
    apply_task_control_effect,
    configure,
    execute_task,
    execute_task_effect,
    materialize_goal_effect,
    prepare_task,
    prepare_task_effect,
)
from aether_worker.research import estimate_tokens, research_prompt
from aether_worker.outbox import enqueue_task, insert_followup_command
from tests.integration.goal_approval import record_owner_approval
from tests.integration.test_artifacts import _project
from tests.integration.test_worker_process import _temporal_address

FACT = "接口请求上限为每分钟六十次。"
GOAL = "根据资料，接口每分钟最多允许多少次请求？只回答请求限额。"
KEY = "synthetic-audit-key"


def _endpoints(role_urls, root) -> WorkerEndpoints:
    return WorkerEndpoints(
        worker_url=role_urls["aether_worker"],
        verifier_url=role_urls["aether_verifier"],
        dispatcher_url=role_urls["aether_dispatcher"],
        artifact_root=root,
        temporal_address="127.0.0.1:9",
        task_queue="round4-direct",
        research_provider="live",
        live_model_base="http://never-contacted.invalid/v1",
        live_model_alias="audit-model",
        live_model_key=KEY,
    )


def _completion(state):
    def completion(**_kwargs):
        state["calls"] += 1
        if state["delay"]:
            time.sleep(state["delay"])
        if state["callback"] is not None:
            state["callback"]()
        hidden = {} if state["cost"] is None else {"response_cost": state["cost"]}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=state["output"]))],
            model="audit-model",
            usage=SimpleNamespace(prompt_tokens=state["tokens"], completion_tokens=state["completion_tokens"]),
            _hidden_params=hidden,
        )

    return completion


def setup_case(client, role_urls, monkeypatch, *, sources=(FACT,), goal=GOAL, budget=None):
    owner, project, _headers = _project(client)
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, database)
    refs = [store.put(owner_id=owner, project_id=project, data=value.encode(), media_type="text/plain") for value in sources]
    created = enqueue_task(
        database,
        owner_id=owner,
        project_id=project,
        goal=goal,
        command_key=str(uuid.uuid4()),
        budget=budget,
        allowed_source_ids=[str(ref["artifact_id"]) for ref in refs],
    )
    record_owner_approval(
        database,
        owner_id=owner,
        project_id=project,
        task_id=created["task_id"],
        workflow_id=created["workflow_id"],
    )
    endpoints = _endpoints(role_urls, store.root)
    payload = {"project_id": project, "task_id": created["task_id"], "command_id": created["command_key"]}
    state = {"calls": 0, "output": f"资料记载：{FACT}", "tokens": 100, "completion_tokens": 20, "cost": 0.001, "delay": 0, "callback": None}
    monkeypatch.setattr(litellm, "completion", _completion(state))
    return owner, project, database, store, refs, endpoints, payload, state


def test_round4_input_budget_applies_before_dispatch(client, role_urls, monkeypatch) -> None:
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        sources=(FACT + "\n" + "token " * 2000,),
        budget={"max_tokens": 100, "max_cost_micro_usd": 2_000_000, "max_model_requests": 4, "active_seconds": 1800, "verification_reserve_fraction": 0.2},
    )
    state["tokens"] = 2500
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0, result


def test_round4_known_tokens_survive_unknown_price(client, role_urls, monkeypatch) -> None:
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["cost"] = None
    materialize_goal_effect(endpoints, payload)
    with database.transaction(owner_id=owner, project_id=project) as conn:
        used = conn.execute(
            text("SELECT tokens_used FROM budget_accounts WHERE project_id = :project AND task_id = :task"),
            {"project": project, "task": payload["task_id"]},
        ).scalar_one()
    assert used == 120


def test_round4_reported_cost_above_reservation_is_kept(client, role_urls, monkeypatch, aether_admin) -> None:
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        budget={"max_cost_micro_usd": 2_000_000, "max_tokens": 100_000, "max_model_requests": 4, "active_seconds": 1800, "verification_reserve_fraction": 0.2},
    )
    state["cost"] = 0.15
    result = materialize_goal_effect(endpoints, payload)
    with database.transaction(owner_id=owner, project_id=project) as conn:
        actual = conn.execute(
            text("SELECT actual_micro_usd, usage_status FROM model_requests WHERE project_id = :project AND task_id = :task"),
            {"project": project, "task": payload["task_id"]},
        ).one()
    assert actual == (150_000, "observed")
    assert result["status"] != "SUCCEEDED"
    snapshot = BudgetLedger(database).snapshot(owner_id=owner, project_id=project, task_id=payload["task_id"])
    assert snapshot["observed_micro_usd"] == 150_000
    assert snapshot["observed_request_count"] == 1
    assert snapshot["actual_known_micro_usd"] == 0
    task_body = client.get(f"/v1/projects/{project}/tasks/{payload['task_id']}").json()
    assert task_body["budget"]["observed_micro_usd"] == 150_000
    assert task_body["budget"]["actual_known_micro_usd"] == 0
    assert task_body["budget"]["unknown_request_count"] == 0
    costs = client.get(f"/v1/projects/{project}/costs").json()
    assert costs["observed_micro_usd"] == 150_000
    assert costs["actual_known_micro_usd"] == 0
    _downgrade_maps_the_observed_row(aether_admin, project, payload["task_id"])


def test_round4_deadline_is_checked_after_the_provider_returns(client, role_urls, monkeypatch) -> None:
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        budget={"max_cost_micro_usd": 2_000_000, "max_tokens": 100_000, "max_model_requests": 4, "active_seconds": 1, "verification_reserve_fraction": 0.2},
    )
    state["delay"] = 1.15
    result = materialize_goal_effect(endpoints, payload)
    assert result["status"] != "SUCCEEDED"


@pytest.mark.parametrize("cost", [0.001, None])
def test_round4_crash_after_response_does_not_redispatch(client, role_urls, monkeypatch, cost) -> None:
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["cost"] = cost
    original = ArtifactStore.put
    armed = True

    def fail_once(self, **kwargs):
        nonlocal armed
        if armed and kwargs.get("classification") == "task_answer":
            armed = False
            raise OSError("injected failure after response")
        return original(self, **kwargs)

    monkeypatch.setattr(ArtifactStore, "put", fail_once)
    with pytest.raises(OSError):
        materialize_goal_effect(endpoints, payload)
    error = None
    try:
        execute_task_effect(endpoints, payload)
    except Exception as exc:
        error = exc
    assert state["calls"] == 1, (state["calls"], repr(error))


def test_round4_pause_during_the_call_reuses_the_response(client, role_urls, monkeypatch) -> None:
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["cost"] = None

    def pause() -> None:
        with database.transaction(owner_id=owner, project_id=project) as conn:
            conn.execute(text("UPDATE tasks SET resume_state = 'pause_requested' WHERE id = :id"), {"id": payload["task_id"]})

    state["callback"] = pause
    first = materialize_goal_effect(endpoints, payload)
    assert first["status"] == "PAUSED"
    assert state["calls"] == 1
    state["callback"] = None
    with database.transaction(owner_id=owner, project_id=project) as conn:
        workflow_id = conn.execute(text("SELECT workflow_id FROM tasks WHERE id = :id"), {"id": payload["task_id"]}).scalar_one()
        insert_followup_command(conn, project_id=project, task_id=payload["task_id"], workflow_id=str(workflow_id), command_key="round4-resume", kind="resume")
    resumed = apply_task_control_effect(endpoints, {"project_id": project, "task_id": payload["task_id"], "command_id": "round4-resume"})
    assert resumed["resumed"] is True
    execute_task_effect(endpoints, payload)
    assert state["calls"] == 1


def test_round4_dispatch_without_a_response_is_not_retried(client, role_urls, monkeypatch) -> None:
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)

    def fail(**_kwargs):
        state["calls"] += 1
        raise RuntimeError("transport dropped the response")

    monkeypatch.setattr(litellm, "completion", fail)
    first = materialize_goal_effect(endpoints, payload)
    assert first["status"] != "SUCCEEDED"
    second = execute_task_effect(endpoints, payload)
    assert state["calls"] == 1
    assert second["status"] != "SUCCEEDED"


@pytest.mark.parametrize(
    "answer",
    [
        "接口每分钟最多允许的请求",
        "引用：接口每分钟最多允许的请求数量明确规定为六十次。\n最终答案：六百次。",
    ],
)
def test_round4_unanswered_or_contradictory_goal_not_success(client, role_urls, monkeypatch, answer) -> None:
    source = "接口每分钟最多允许的请求数量明确规定为六十次。"
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch, sources=(source,))
    state["output"] = answer
    assert materialize_goal_effect(endpoints, payload)["status"] != "SUCCEEDED"


def test_round4_multi_part_goal_requires_every_overlapping_fact(client, role_urls, monkeypatch) -> None:
    source = FACT + "\n实验设备每九十天完成一次保养。"
    goal = "请根据资料分别回答接口请求上限和实验设备保养周期。"
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch, sources=(source,), goal=goal)
    state["output"] = FACT
    assert materialize_goal_effect(endpoints, payload)["status"] != "SUCCEEDED"


def test_round4_corrupt_explicit_source_blocks_dispatch(client, role_urls, monkeypatch) -> None:
    _owner, project, _database, store, refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        sources=(FACT, "设备每九十天必须保养一次。"),
    )
    bad = refs[1]
    (store.root / project / str(bad["sha256"])[:2] / str(bad["sha256"])).write_bytes(b"corrupt")
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0, result
    assert result["status"] == "NEEDS_REVIEW"


def test_round4_async_activity_remains_responsive(client, role_urls, monkeypatch) -> None:
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    configure(endpoints)
    assert prepare_task_effect(endpoints, payload)["status"] == "prepared"
    state["delay"] = 0.4

    async def scenario() -> float:
        fired: list[float] = []

        async def ticker() -> None:
            await asyncio.sleep(0.05)
            fired.append(time.monotonic())

        start = time.monotonic()
        waiting = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        await execute_task(payload)
        await waiting
        return fired[0] - start

    assert asyncio.run(scenario()) < 0.25


def test_round4_slow_call_does_not_block_another_temporal_task(client, role_urls, monkeypatch) -> None:
    address = _temporal_address()
    owner, project, _headers = _project(client)
    database = Database(role_urls["aether_worker"])
    root = client.app.state.settings.artifact_root
    store = ArtifactStore(root, database)
    slow_fact = "慢任务的水位是每小时四十升。SLOWMARK"
    fast_fact = "快任务的水位是每小时四升。"
    slow_ref = store.put(owner_id=owner, project_id=project, data=slow_fact.encode(), media_type="text/plain")
    fast_ref = store.put(owner_id=owner, project_id=project, data=fast_fact.encode(), media_type="text/plain")
    slow = enqueue_task(
        database,
        owner_id=owner,
        project_id=project,
        goal="根据资料回答慢任务的水位。",
        command_key=str(uuid.uuid4()),
        allowed_source_ids=[str(slow_ref["artifact_id"])],
    )
    fast = enqueue_task(
        database,
        owner_id=owner,
        project_id=project,
        goal="根据资料回答快任务的水位。",
        command_key=str(uuid.uuid4()),
        allowed_source_ids=[str(fast_ref["artifact_id"])],
    )
    record_owner_approval(database, owner_id=owner, project_id=project, task_id=slow["task_id"], workflow_id=slow["workflow_id"])
    record_owner_approval(database, owner_id=owner, project_id=project, task_id=fast["task_id"], workflow_id=fast["workflow_id"])
    state = {"calls": 0}

    def completion(**kwargs):
        state["calls"] += 1
        content = kwargs["messages"][0]["content"]
        if "SLOWMARK" in content:
            time.sleep(1.2)
            output = f"资料记载：{slow_fact}"
        else:
            output = f"资料记载：{fast_fact}"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output))],
            model="audit-model",
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=12),
            _hidden_params={"response_cost": 0.001},
        )

    monkeypatch.setattr(litellm, "completion", completion)
    configure(_endpoints(role_urls, root))
    queue = f"round4-parallel-{uuid.uuid4()}"

    async def run() -> float:
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task]):

            async def launch(created: dict):
                handle = await temporal.start_workflow(
                    DurableTaskWorkflow.run,
                    {"task_id": created["task_id"], "intake_pause_seconds": 0},
                    id=created["workflow_id"],
                    task_queue=queue,
                )
                await handle.signal(DurableTaskWorkflow.command, {"command_id": created["command_key"], "project_id": project, "kind": "start"})
                await handle.signal(DurableTaskWorkflow.close)
                return handle

            slow_handle = await launch(slow)
            await asyncio.sleep(0.3)
            started = time.monotonic()
            fast_handle = await launch(fast)
            await asyncio.wait_for(fast_handle.result(), timeout=1.0)
            elapsed = time.monotonic() - started
            await slow_handle.result()
            return elapsed

    elapsed = asyncio.run(run())
    assert elapsed < 1.0
    assert state["calls"] >= 2


def _migration_0011():
    path = Path("migrations/versions/0011_model_call_lifecycle.py")
    spec = importlib.util.spec_from_file_location("model_call_lifecycle_0011", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _downgrade_maps_the_observed_row(admin_url: str, project_id: str, task_id: str) -> None:
    module = _migration_0011()
    with psycopg.connect(admin_url) as conn:
        conn.execute(module.MAP_OBSERVED_BEFORE_DOWNGRADE)
        for statement in module.RESTORE_LEGACY_USAGE_CHECKS:
            conn.execute(statement)
        row = conn.execute(
            "SELECT usage_status, actual_micro_usd, usage_detail FROM model_requests WHERE project_id = %s AND task_id = %s",
            (project_id, task_id),
        ).fetchone()
        assert row is not None
        status, actual, detail = row
        if isinstance(detail, str):
            detail = json.loads(detail)
        assert status == "unknown"
        assert actual is None
        assert detail["downgraded_from"] == "observed"
        assert int(detail["reported_cost_micro_usd"]) == 150_000
        conn.rollback()


def test_codex_money_below_the_request_ceiling_is_not_sent(client, role_urls, monkeypatch) -> None:
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        budget={"max_cost_micro_usd": 80, "max_tokens": 100_000, "max_model_requests": 4, "active_seconds": 1800, "verification_reserve_fraction": 0},
    )
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0, result
    assert result["stop_reason"] == "budget_exhausted"
    with database.transaction(owner_id=owner, project_id=project) as conn:
        count = conn.execute(text("SELECT count(*) FROM model_requests WHERE task_id = :task"), {"task": payload["task_id"]}).scalar_one()
    assert count == 0


def test_codex_raw_token_budget_does_not_cover_chat_framing(client, role_urls, monkeypatch) -> None:
    prompt = research_prompt(goal=GOAL, sources=[FACT])
    raw = estimate_tokens(prompt)
    _owner, _project_id, _database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        budget={"max_cost_micro_usd": 2_000_000, "max_tokens": raw + 1, "max_model_requests": 4, "active_seconds": 1800, "verification_reserve_fraction": 0},
    )
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0, result
    assert result["stop_reason"] == "budget_exhausted"


def test_codex_empty_content_still_books_known_usage(client, role_urls, monkeypatch) -> None:
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["output"] = ""
    state["tokens"] = 40
    state["completion_tokens"] = 0
    state["cost"] = 0.001
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 1
    assert result["status"] == "NEEDS_REVIEW"
    with database.transaction(owner_id=owner, project_id=project) as conn:
        account = conn.execute(
            text("SELECT tokens_used, spent_micro_usd FROM budget_accounts WHERE project_id = :project AND task_id = :task"),
            {"project": project, "task": payload["task_id"]},
        ).one()
        usage = conn.execute(
            text("SELECT usage_status, input_tokens FROM model_requests WHERE project_id = :project AND task_id = :task"),
            {"project": project, "task": payload["task_id"]},
        ).one()
    assert account == (40, 1000)
    assert usage == ("known", 40)


def test_codex_faithful_paraphrase_can_succeed(client, role_urls, monkeypatch) -> None:
    """A same-value paraphrase is not a failed citation. Swapped subjects still fail in the round-5 tests."""
    _owner, _project, _database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["output"] = "每分钟六十次。"
    result = materialize_goal_effect(endpoints, payload)
    assert result["status"] == "SUCCEEDED"


def test_codex_zero_remaining_time_does_not_dispatch(client, role_urls, monkeypatch) -> None:
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    original = BudgetLedger.prepare_model_call

    def backdate(self, **kwargs):
        prepared = original(self, **kwargs)
        with database.transaction(owner_id=owner, project_id=project) as conn:
            conn.execute(
            text(
                "UPDATE task_events SET created_at = now() - interval '3 hours' "
                "WHERE task_id = :id AND event_type = 'task.active' AND payload->>'phase' = 'start'"
            ),
            {"id": payload["task_id"]},
        )
        return prepared

    monkeypatch.setattr(BudgetLedger, "prepare_model_call", backdate)
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0, result
    assert result["stop_reason"] == "budget_exhausted"
    with database.transaction(owner_id=owner, project_id=project) as conn:
        usage = conn.execute(
            text("SELECT usage_status, actual_micro_usd FROM model_requests WHERE task_id = :task"),
            {"task": payload["task_id"]},
        ).one()
        held = conn.execute(
            text("SELECT held_micro_usd FROM budget_accounts WHERE task_id = :task"),
            {"task": payload["task_id"]},
        ).scalar_one()
    assert usage == ("cancelled_before_dispatch", None)
    assert held == 0


def test_codex_missing_explicit_file_is_not_dispatched(client, role_urls, monkeypatch) -> None:
    _owner, project, _database, store, refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    stored = refs[0]
    path = store.root / project / str(stored["sha256"])[:2] / str(stored["sha256"])
    assert path.is_file()
    path.unlink()
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0, result
    assert result["status"] == "NEEDS_REVIEW"


async def signal_after_the_provider_starts(*, address, endpoints, project, workflow_id, payload, state, queue, signals, timeout=30) -> None:
    """Start one live task and deliver extra workflow signals once the provider call has begun."""
    configure(endpoints)
    temporal = await Client.connect(address)
    async with Worker(
        temporal,
        task_queue=queue,
        workflows=[DurableTaskWorkflow],
        activities=[prepare_task, execute_task, apply_task_control],
    ):
        handle = await temporal.start_workflow(
            DurableTaskWorkflow.run,
            {"task_id": payload["task_id"], "intake_pause_seconds": 0},
            id=str(workflow_id),
            task_queue=queue,
        )
        await handle.signal(
            DurableTaskWorkflow.command,
            {"command_id": payload["command_id"], "project_id": project, "kind": "start"},
        )
        for _ in range(80):
            if state["calls"] >= 1:
                break
            await asyncio.sleep(0.05)
        assert state["calls"] >= 1
        for signal in signals:
            await handle.signal(DurableTaskWorkflow.command, signal)
        await handle.signal(DurableTaskWorkflow.close)
        await asyncio.wait_for(handle.result(), timeout=timeout)


@pytest.mark.parametrize("kind", ["pause", "cancel"])
def test_codex_workflow_signal_during_the_call_blocks_success(client, role_urls, monkeypatch, kind: str) -> None:
    address = _temporal_address()
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["delay"] = 3.0
    state["output"] = f"资料记载：{FACT}"
    command_key = f"round4-during-{kind}"
    with database.transaction(owner_id=owner, project_id=project) as conn:
        workflow_id = conn.execute(text("SELECT workflow_id FROM tasks WHERE id = :id"), {"id": payload["task_id"]}).scalar_one()
        insert_followup_command(
            conn,
            project_id=project,
            task_id=payload["task_id"],
            workflow_id=str(workflow_id),
            command_key=command_key,
            kind=kind,
        )
    asyncio.run(
        signal_after_the_provider_starts(
            address=address,
            endpoints=endpoints,
            project=project,
            workflow_id=workflow_id,
            payload=payload,
            state=state,
            queue=f"round4-signal-{uuid.uuid4()}",
            signals=[{"command_id": command_key, "project_id": project, "kind": kind}],
        )
    )
    with database.transaction(owner_id=owner, project_id=project) as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": payload["task_id"]}).scalar_one()
        recorded = conn.execute(
            text("SELECT usage_detail->>'dispatch_state' FROM model_requests WHERE task_id = :id"),
            {"id": payload["task_id"]},
        ).scalar_one()
    assert state["calls"] == 1
    assert recorded == "response_recorded"
    assert status == ("PAUSED" if kind == "pause" else "CANCELLED")
