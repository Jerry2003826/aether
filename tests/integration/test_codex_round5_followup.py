"""Follow-up regressions for the Codex review of the merged round-5 repair."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import litellm
from sqlalchemy import text
from aether_storage.budget import BudgetLedger
from aether_worker.goal_effect import (
    INPUT_MICRO_USD_PER_TOKEN,
    OUTPUT_MICRO_USD_PER_TOKEN,
    apply_task_control_effect,
    materialize_goal_effect,
)
from aether_worker.outbox import insert_followup_command
from aether_worker.research import estimate_provider_request_tokens, research_prompt
from tests.integration.test_audit_round4 import FACT, GOAL, setup_case, signal_after_the_provider_starts
from tests.integration.test_worker_process import _temporal_address

SOURCE = "甲接口每分钟最多接受60次请求。\n乙接口每分钟最多接受90次请求。"
SWAP_GOAL = "根据资料，分别说明甲接口和乙接口每分钟最多接受多少次请求。"


def test_subjectless_multi_object_answer_is_not_success(client, role_urls, monkeypatch) -> None:
    _owner, _project, _database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        sources=(SOURCE,),
        goal=SWAP_GOAL,
    )
    state["output"] = "每分钟最多60次，每分钟最多90次"
    assert materialize_goal_effect(endpoints, payload)["status"] != "SUCCEEDED"


def test_shared_limit_word_does_not_cover_a_second_object(client, role_urls, monkeypatch) -> None:
    fact = "客户端请求限额为每分钟60次。"
    goal = "分别说明客户端限额和服务端限额"
    _owner, _project, _database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        sources=(fact,),
        goal=goal,
    )
    state["output"] = f"资料记载：{fact}"
    assert materialize_goal_effect(endpoints, payload)["status"] != "SUCCEEDED"


def test_pause_behind_a_non_control_command_still_stops_the_call(client, role_urls, monkeypatch) -> None:
    address = _temporal_address()
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["delay"] = 3.0
    state["output"] = f"资料记载：{FACT}"
    command_key = "followup-pause-behind-approve"
    with database.transaction(owner_id=owner, project_id=project) as conn:
        workflow_id = conn.execute(text("SELECT workflow_id FROM tasks WHERE id = :id"), {"id": payload["task_id"]}).scalar_one()
        insert_followup_command(
            conn,
            project_id=project,
            task_id=payload["task_id"],
            workflow_id=str(workflow_id),
            command_key=command_key,
            kind="pause",
        )
    asyncio.run(
        signal_after_the_provider_starts(
            address=address,
            endpoints=endpoints,
            project=project,
            workflow_id=workflow_id,
            payload=payload,
            state=state,
            queue=f"followup-control-{uuid.uuid4()}",
            signals=[
                {"command_id": "approve-before-pause", "project_id": project, "kind": "approve_goal"},
                {"command_id": command_key, "project_id": project, "kind": "pause"},
            ],
            timeout=20,
        )
    )
    with database.transaction(owner_id=owner, project_id=project) as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": payload["task_id"]}).scalar_one()
    assert state["calls"] == 1
    assert status == "PAUSED"


def test_stop_after_the_dispatch_permit_does_not_call_the_provider(client, role_urls, monkeypatch) -> None:
    owner, project, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    original = BudgetLedger.acquire_dispatch_permit

    def acquire_then_stop(self, **kwargs):
        acquired = original(self, **kwargs)
        key = str(uuid.uuid4())
        with database.transaction(owner_id=owner, project_id=project) as conn:
            workflow_id = conn.execute(text("SELECT workflow_id FROM tasks WHERE id = :id"), {"id": payload["task_id"]}).scalar_one()
            insert_followup_command(
                conn,
                project_id=project,
                task_id=payload["task_id"],
                workflow_id=str(workflow_id),
                command_key=key,
                kind="pause",
            )
        stopped = apply_task_control_effect(endpoints, {"project_id": project, "task_id": payload["task_id"], "command_id": key})
        assert stopped["status"] == "PAUSED"
        assert acquired is True
        return acquired

    monkeypatch.setattr(BudgetLedger, "acquire_dispatch_permit", acquire_then_stop)
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0, result
    assert result["status"] == "PAUSED"


def test_output_cap_shrinks_to_the_remaining_money(client, role_urls, monkeypatch) -> None:
    prompt = research_prompt(goal=GOAL, sources=[FACT])
    estimate = estimate_provider_request_tokens(prompt)
    affordable = 4
    budget = estimate * INPUT_MICRO_USD_PER_TOKEN + affordable * OUTPUT_MICRO_USD_PER_TOKEN
    _owner, _project, _database, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        budget={
            "max_cost_micro_usd": budget,
            "max_tokens": 100_000,
            "max_model_requests": 4,
            "active_seconds": 1800,
            "verification_reserve_fraction": 0,
        },
    )

    def completion(**kwargs):
        state["calls"] += 1
        state["max_tokens"] = kwargs.get("max_tokens")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=f"资料记载：{FACT}"))],
            model="audit-model",
            usage=SimpleNamespace(prompt_tokens=state["tokens"], completion_tokens=state["completion_tokens"]),
            _hidden_params={"response_cost": 0.001},
        )

    monkeypatch.setattr(litellm, "completion", completion)
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 1, result
    assert state["max_tokens"] == affordable
    assert result["status"] == "SUCCEEDED"
