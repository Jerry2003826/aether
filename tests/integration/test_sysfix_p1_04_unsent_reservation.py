"""P1-04: a reservation that never left the process is reused or released, never frozen as unknown.

INV-MONEY-02. Only dispatch_state='sending' may become unknown on a retry.
Ported from reports/audits/system_round1/repro/test_p1_04_unsent_reservation.py.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import aether_worker.goal_effect as ge
from aether_domain.security import sha256_text
from aether_gateway.model import ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from tests.integration.sysfix_b_helpers import project, rows
from tests.integration.test_audit_round4 import setup_case

LEDGER_SQL = "SELECT usage_status, usage_detail->>'dispatch_state' AS dispatch FROM model_requests WHERE task_id = :t"
ACCOUNT_SQL = "SELECT held_micro_usd, spent_micro_usd, tokens_held FROM budget_accounts WHERE task_id = :t"


@pytest.mark.parametrize("where", ["issue_token", "consume_dispatch_permit"])
def test_unsent_reservation_is_reused_after_a_retry(client, role_urls, monkeypatch, where) -> None:
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    target = ModelGateway if where == "issue_token" else BudgetLedger
    original = getattr(target, where)
    tripped: list[bool] = []

    def flaky(self, **kwargs):
        if not tripped:
            tripped.append(True)
            raise RuntimeError("transient database error before the provider call")
        return original(self, **kwargs)

    monkeypatch.setattr(target, where, flaky)
    with pytest.raises(RuntimeError):
        ge.materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 0
    before = rows(database, owner, project_id, LEDGER_SQL, t=payload["task_id"])
    result = ge.execute_task_effect(endpoints, payload)
    after = rows(database, owner, project_id, LEDGER_SQL, t=payload["task_id"])
    account = rows(database, owner, project_id, ACCOUNT_SQL, t=payload["task_id"])[0]
    detail = (
        f"where={where} 重试前={before} 重试后={after} 结果={result} "
        f"held={account['held_micro_usd']} spent={account['spent_micro_usd']} provider_calls={state['calls']}"
    )
    assert after[0]["usage_status"] != "unknown", detail
    assert result["status"] == "SUCCEEDED", detail
    assert state["calls"] == 1, detail
    assert account["held_micro_usd"] == 0, detail


def test_a_sending_request_stays_unknown_and_is_not_resent(client, role_urls, monkeypatch) -> None:
    """The provider may have billed it. Retrying must not send it a second time or release the hold."""
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    original = BudgetLedger.record_model_response
    tripped: list[bool] = []

    def crash_after_send(self, **kwargs):
        if not tripped:
            tripped.append(True)
            raise RuntimeError("worker died after the provider answered")
        return original(self, **kwargs)

    monkeypatch.setattr(BudgetLedger, "record_model_response", crash_after_send)
    with pytest.raises(RuntimeError):
        ge.materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 1
    assert rows(database, owner, project_id, LEDGER_SQL, t=payload["task_id"]) == [{"usage_status": "reserved", "dispatch": "sending"}]
    result = ge.execute_task_effect(endpoints, payload)
    after = rows(database, owner, project_id, LEDGER_SQL, t=payload["task_id"])
    account = rows(database, owner, project_id, ACCOUNT_SQL, t=payload["task_id"])[0]
    assert state["calls"] == 1, result
    assert after[0]["usage_status"] == "unknown", after
    assert result.get("stop_reason") == "live_call_unreconciled", result
    assert account["held_micro_usd"] > 0, account


def test_gateway_denial_before_send_releases_the_reservation(client, role_urls, monkeypatch) -> None:
    """S-030: the gateway refused before contacting the provider, so nothing can have been billed."""
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    monkeypatch.setattr(ModelGateway, "issue_token", lambda self, **kwargs: "not-a-token-the-gateway-issued")
    result = ge.materialize_goal_effect(endpoints, payload)
    after = rows(database, owner, project_id, LEDGER_SQL, t=payload["task_id"])
    account = rows(database, owner, project_id, ACCOUNT_SQL, t=payload["task_id"])[0]
    detail = f"结果={result} 账本={after} held={account['held_micro_usd']} provider_calls={state['calls']}"
    assert state["calls"] == 0, detail
    assert after[0]["usage_status"] == "cancelled_before_dispatch", detail
    assert account["held_micro_usd"] == 0, detail
    assert account["tokens_held"] == 0, detail
    assert result["status"] == "NEEDS_REVIEW", detail


def test_a_stale_attempt_cannot_take_over_a_newer_reservation(client, role_urls, monkeypatch) -> None:
    """S-012: an attempt that lost its reservation to a retry may not flip the retry's row or call the provider."""
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    original = BudgetLedger.acquire_dispatch_permit
    nested: dict[str, object] = {}
    outcomes: list[bool] = []

    def overtaken(self, **kwargs):
        if "result" not in nested:
            nested["result"] = "running"
            nested["result"] = ge.execute_task_effect(endpoints, payload)
        acquired = original(self, **kwargs)
        outcomes.append(acquired)
        return acquired

    monkeypatch.setattr(BudgetLedger, "acquire_dispatch_permit", overtaken)
    stale = ge.materialize_goal_effect(endpoints, payload)
    after = rows(database, owner, project_id, "SELECT usage_status FROM model_requests WHERE task_id = :t", t=payload["task_id"])
    account = rows(database, owner, project_id, ACCOUNT_SQL, t=payload["task_id"])[0]
    detail = f"新尝试={nested['result']} 旧尝试={stale} 许可={outcomes} 账本={after} held={account['held_micro_usd']} provider_calls={state['calls']}"
    assert nested["result"]["status"] == "SUCCEEDED", detail
    assert outcomes == [True, False], detail
    assert state["calls"] == 1, detail
    assert after == [{"usage_status": "known"}], detail
    assert account["held_micro_usd"] == 0, detail


def test_dispatch_permit_belongs_to_the_attempt_holding_the_reservation(client, role_urls) -> None:
    owner, project_id, _headers = project(client)
    database = Database(role_urls["aether_worker"])
    with database.transaction(owner_id=owner, project_id=project_id) as conn:
        task_id = conn.execute(
            text("INSERT INTO tasks (project_id, original_goal, workflow_id, status) VALUES (:p, '派发', :w, 'RUNNING') RETURNING id::text"),
            {"p": project_id, "w": f"holder-{uuid.uuid4()}"},
        ).scalar_one()
    ledger = BudgetLedger(database)
    ledger.open_account(owner_id=owner, project_id=project_id, task_id=task_id, limit_micro_usd=2_000_000, token_limit=100_000, max_requests=4)
    scope = {"owner_id": owner, "project_id": project_id, "task_id": task_id, "request_key": "research-holder"}
    call = {"role": "research", "model_alias": "audit-model", "prompt_sha256": sha256_text("提示"), "source_snapshot_sha256": sha256_text("")}
    first = ledger.prepare_model_call(**scope, **call, amount=9_000, tokens=60, output_cap=20, attempt_id="attempt-a")
    second = ledger.prepare_model_call(**scope, **call, amount=8_000, tokens=50, output_cap=10, attempt_id="attempt-b")
    assert (first["action"], second["action"]) == ("send", "send")
    held = rows(database, owner, project_id, ACCOUNT_SQL, t=task_id)[0]
    assert (held["held_micro_usd"], held["tokens_held"]) == (8_000, 50)
    assert rows(database, owner, project_id, "SELECT requests_admitted FROM budget_accounts WHERE task_id = :t", t=task_id)[0]["requests_admitted"] == 1
    assert ledger.acquire_dispatch_permit(**scope, attempt_id="attempt-a") is False
    assert ledger.acquire_dispatch_permit(**scope, attempt_id="attempt-b") is True
    assert ledger.consume_dispatch_permit(**scope, attempt_id="attempt-a") is False
    ledger.release_unsent(**scope, attempt_id="attempt-a")
    assert rows(database, owner, project_id, LEDGER_SQL, t=task_id) == [{"usage_status": "reserved", "dispatch": "permitted"}]
    assert ledger.consume_dispatch_permit(**scope, attempt_id="attempt-b") is True
    ledger.release_unsent(**scope, attempt_id="attempt-b")
    assert rows(database, owner, project_id, LEDGER_SQL, t=task_id) == [{"usage_status": "reserved", "dispatch": "sending"}]
    assert rows(database, owner, project_id, ACCOUNT_SQL, t=task_id)[0]["held_micro_usd"] == 8_000
