"""P1-04 · 还没发出的模型请求在重试时被当成"结果未知"，预算永久冻结、任务转人工。

不变量 INV-MONEY-02：确定没有离开进程的预留必须能释放或复用；
只有 dispatch_state='sending' 之后的请求才允许记成 unknown。
"""

from __future__ import annotations

import pytest

import aether_worker.goal_effect as ge
from aether_gateway.model import ModelGateway
from aether_storage.budget import BudgetLedger
from audit_helpers import rows
from tests.integration.test_audit_round4 import setup_case


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
    before = rows(database, owner, project_id, "SELECT usage_status, usage_detail->>'dispatch_state' AS dispatch FROM model_requests WHERE task_id = :t", t=payload["task_id"])
    result = ge.execute_task_effect(endpoints, payload)
    after = rows(database, owner, project_id, "SELECT usage_status, usage_detail->>'dispatch_state' AS dispatch FROM model_requests WHERE task_id = :t", t=payload["task_id"])
    account = rows(database, owner, project_id, "SELECT held_micro_usd, spent_micro_usd FROM budget_accounts WHERE task_id = :t", t=payload["task_id"])[0]
    detail = (
        f"where={where} 重试前={before} 重试后={after} 结果={result} "
        f"held={account['held_micro_usd']} spent={account['spent_micro_usd']} provider_calls={state['calls']}"
    )
    assert after[0]["usage_status"] != "unknown", detail
    assert result["status"] == "SUCCEEDED", detail
    assert state["calls"] == 1, detail
    assert account["held_micro_usd"] == 0, detail
