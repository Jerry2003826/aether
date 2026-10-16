"""Round 5 regressions on real PostgreSQL. Only the provider transport is replaced."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

import aether_worker.goal_effect as ge
from aether_gateway.model import ModelGateway
from aether_worker.outbox import insert_followup_command
from tests.integration.test_audit_round4 import setup_case

SOURCE = "甲接口每分钟最多接受60次请求。\n乙接口每分钟最多接受90次请求。"
GOAL = "根据资料，分别说明甲接口和乙接口每分钟最多接受多少次请求。"


def _one(db, owner, project, sql, **params):
    with db.transaction(owner_id=owner, project_id=project) as conn:
        row = conn.execute(text(sql), params).mappings().one()
    return dict(row)


@pytest.mark.parametrize(
    ("source", "goal", "answer"),
    [
        (SOURCE, GOAL, "甲接口每分钟允许90次请求，乙接口每分钟允许60次请求。"),
        ("客户端每分钟最多发送60次请求。", "根据资料，客户端每分钟请求限额是多少？", "客户端每秒最多发送60次请求。"),
        ("客户端每分钟最多发送60次请求。", "根据资料，客户端每分钟请求限额是多少？", "客户端每分钟可以超过60次请求。"),
        ("客户端每分钟最多发送60次请求。", "根据资料，分别给出客户端请求限额和请求超时时间。", "资料记载：客户端每分钟最多发送60次请求。"),
    ],
)
def test_r501_semantic_mismatch_is_not_success(client, role_urls, monkeypatch, source, goal, answer) -> None:
    *_rest, endpoints, payload, state = setup_case(client, role_urls, monkeypatch, sources=(source,), goal=goal)
    state["output"] = answer
    assert ge.materialize_goal_effect(endpoints, payload)["status"] != "SUCCEEDED"


def test_r501_positive_correct_paraphrase(client, role_urls, monkeypatch) -> None:
    *_rest, endpoints, payload, state = setup_case(client, role_urls, monkeypatch, sources=(SOURCE,), goal=GOAL)
    state["output"] = "甲接口每分钟允许60次请求，乙接口每分钟允许90次请求。"
    assert ge.materialize_goal_effect(endpoints, payload)["status"] == "SUCCEEDED"


@pytest.mark.parametrize("cost", [0.001, None])
def test_r502_empty_text_keeps_usage_receipt(client, role_urls, monkeypatch, cost) -> None:
    owner, project, db, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["output"] = ""
    state["cost"] = cost
    result = ge.materialize_goal_effect(endpoints, payload)
    row = _one(db, owner, project, "SELECT * FROM model_requests WHERE project_id=:p AND task_id=:t", p=project, t=payload["task_id"])
    account = _one(db, owner, project, "SELECT * FROM budget_accounts WHERE project_id=:p AND task_id=:t", p=project, t=payload["task_id"])
    assert result["status"] != "SUCCEEDED"
    assert row["input_tokens"] == 100 and row["output_tokens"] == 20
    assert account["tokens_used"] == 120
    detail = row["usage_detail"]
    assert detail["dispatch_state"] == "response_recorded"
    if cost is not None:
        assert row["actual_micro_usd"] == 1000


def test_r502_overrun_books_aggregate_usage(client, role_urls, monkeypatch) -> None:
    owner, project, db, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["tokens"] = 2000
    result = ge.materialize_goal_effect(endpoints, payload)
    account = _one(db, owner, project, "SELECT * FROM budget_accounts WHERE project_id=:p AND task_id=:t", p=project, t=payload["task_id"])
    assert result["status"] != "SUCCEEDED"
    assert account["tokens_used"] == 2020


@pytest.mark.parametrize("kind", ["pause", "cancel"])
def test_r503_stop_applied_before_send_blocks_transport(client, role_urls, monkeypatch, kind) -> None:
    owner, project, db, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    original = ModelGateway.issue_token
    stopped = []

    def controlled(self, **kwargs):
        token = original(self, **kwargs)
        key = str(uuid.uuid4())
        with db.transaction(owner_id=owner, project_id=project) as conn:
            workflow_id = conn.execute(
                text("SELECT workflow_id FROM tasks WHERE project_id=:p AND id=:t"),
                {"p": project, "t": payload["task_id"]},
            ).scalar_one()
            insert_followup_command(
                conn,
                project_id=project,
                task_id=payload["task_id"],
                workflow_id=workflow_id,
                command_key=key,
                kind=kind,
            )
        stopped.append(ge.apply_task_control_effect(endpoints, {**payload, "command_id": key}))
        return token

    monkeypatch.setattr(ModelGateway, "issue_token", controlled)
    result = ge.materialize_goal_effect(endpoints, payload)
    assert stopped[0]["status"] == ("PAUSED" if kind == "pause" else "CANCELLED")
    assert state["calls"] == 0, result


@pytest.mark.parametrize("expire", [False, True])
def test_r504_verifier_retry_checks_deadline(client, role_urls, monkeypatch, expire) -> None:
    owner, project, db, _store, _refs, endpoints, payload, state = setup_case(
        client,
        role_urls,
        monkeypatch,
        budget={"active_seconds": 1800},
    )
    original = ge._ensure_verification

    def unavailable(*args, **kwargs):
        raise OSError("round5 verifier unavailable")

    monkeypatch.setattr(ge, "_ensure_verification", unavailable)
    with pytest.raises(OSError, match="round5 verifier unavailable"):
        ge.materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 1
    if expire:
        with db.transaction(owner_id=owner, project_id=project) as conn:
            conn.execute(
                text(
                    "UPDATE task_events SET created_at=:created "
                    "WHERE project_id=:p AND task_id=:t AND event_type='task.active' AND payload->>'phase'='start'"
                ),
                {"created": datetime.now(UTC) - timedelta(hours=2), "p": project, "t": payload["task_id"]},
            )
    monkeypatch.setattr(ge, "_ensure_verification", original)
    result = ge.execute_task_effect(endpoints, payload)
    assert state["calls"] == 1
    if expire:
        assert result["status"] != "SUCCEEDED"
    else:
        assert result["status"] == "SUCCEEDED"


def test_r504_terminal_commit_checks_deadline(client, role_urls, monkeypatch) -> None:
    owner, project, db, _store, _refs, endpoints, payload, _state = setup_case(
        client,
        role_urls,
        monkeypatch,
        budget={"active_seconds": 1800},
    )
    original = ge._ensure_verification

    def expired_after_verification(*args, **kwargs):
        verification = original(*args, **kwargs)
        with db.transaction(owner_id=owner, project_id=project) as conn:
            conn.execute(
                text(
                    "UPDATE task_events SET created_at=:created "
                    "WHERE project_id=:p AND task_id=:t AND event_type='task.active' AND payload->>'phase'='start'"
                ),
                {"created": datetime.now(UTC) - timedelta(hours=2), "p": project, "t": payload["task_id"]},
            )
        return verification

    monkeypatch.setattr(ge, "_ensure_verification", expired_after_verification)
    assert ge.materialize_goal_effect(endpoints, payload)["status"] != "SUCCEEDED"


@pytest.mark.parametrize("prefix", ["", "资料记载：\n"])
def test_r505_correct_quote_does_not_take_source_identity(client, role_urls, monkeypatch, prefix) -> None:
    owner, project, db, store, refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch, sources=(SOURCE,), goal=GOAL)
    state["output"] = prefix + SOURCE
    result = ge.materialize_goal_effect(endpoints, payload)
    texts, _snapshot, status, _missing = ge._source_snapshot(db, store, owner, project, allowed_ids=[str(refs[0]["artifact_id"])])
    assert status == "ok" and texts == [SOURCE]
    assert result["status"] == "SUCCEEDED"
