"""P2-05: a model token serves one booked request of one task and dies with it.

INV-AUTH-03 and INV-MONEY-05. Ported from reports/audits/system_round1/repro/test_p2_05_gateway_token.py,
plus the cancel case (S-029) and a stolen token used for a different request.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import aether_worker.goal_effect as ge
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_worker.outbox import insert_followup_command
from tests.integration.sysfix_b_helpers import rows
from tests.integration.test_audit_round4 import setup_case


def _capture_tokens(monkeypatch) -> list[str]:
    issued: list[str] = []
    original = ModelGateway.issue_token

    def capture(self, **kwargs):
        token = original(self, **kwargs)
        issued.append(token)
        return token

    monkeypatch.setattr(ModelGateway, "issue_token", capture)
    return issued


def test_model_token_dies_with_its_task_and_cannot_bypass_the_ledger(client, role_urls, monkeypatch) -> None:
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    issued = _capture_tokens(monkeypatch)
    assert ge.materialize_goal_effect(endpoints, payload)["status"] == "SUCCEEDED"
    gateway = ModelGateway(database, upstream_base=endpoints.live_model_base, provider_key=endpoints.live_model_key)
    replayed = 0
    for _ in range(3):
        try:
            gateway.complete(issued[0], {"alias": endpoints.live_model_alias, "prompt": "任务已经结束，再来一次", "max_tokens": 16})
            replayed += 1
        except Exception:
            break
    admitted = rows(database, owner, project_id, "SELECT requests_admitted FROM budget_accounts WHERE task_id = :t", t=payload["task_id"])[0]["requests_admitted"]
    detail = f"任务已 SUCCEEDED，旧令牌又成功调用了 {replayed} 次供应商（传输共 {state['calls']} 次），账本 requests_admitted 仍是 {admitted}"
    assert replayed == 0, detail
    assert state["calls"] == 1, detail


def test_model_token_is_revoked_when_the_task_is_cancelled(client, role_urls, monkeypatch) -> None:
    """S-029: cancel lands after the token is issued and before the provider call."""
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    original = ModelGateway.issue_token
    issued: list[str] = []

    def issue_then_cancel(self, **kwargs):
        token = original(self, **kwargs)
        issued.append(token)
        command = str(uuid.uuid4())
        with database.transaction(owner_id=owner, project_id=project_id) as conn:
            workflow_id = conn.execute(text("SELECT workflow_id FROM tasks WHERE id = :t"), {"t": payload["task_id"]}).scalar_one()
            insert_followup_command(conn, project_id=project_id, task_id=payload["task_id"], workflow_id=str(workflow_id), command_key=command, kind="cancel")
        assert ge.apply_task_control_effect(endpoints, {**payload, "command_id": command})["status"] == "CANCELLED"
        return token

    monkeypatch.setattr(ModelGateway, "issue_token", issue_then_cancel)
    result = ge.materialize_goal_effect(endpoints, payload)
    assert result["status"] == "CANCELLED"
    gateway = ModelGateway(database, upstream_base=endpoints.live_model_base, provider_key=endpoints.live_model_key)
    with pytest.raises(GatewayDenied):
        gateway.complete(issued[0], {"alias": endpoints.live_model_alias, "prompt": "取消之后", "max_tokens": 16})
    grants = rows(database, owner, project_id, "SELECT revoked_at FROM capability_grants WHERE task_id = :t", t=payload["task_id"])
    assert grants and all(grant["revoked_at"] is not None for grant in grants), grants
    assert state["calls"] == 0


def test_a_leaked_token_cannot_send_a_different_request(client, role_urls, monkeypatch) -> None:
    """The token is bound to the reserved prompt, alias and output cap, not just to the project."""
    _owner, _project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    issued = _capture_tokens(monkeypatch)
    original = BudgetLedger.consume_dispatch_permit
    attempts: list[str] = []

    def steal_after_consume(self, **kwargs):
        consumed = original(self, **kwargs)
        gateway = ModelGateway(database, upstream_base=endpoints.live_model_base, provider_key=endpoints.live_model_key)
        for body in (
            {"alias": endpoints.live_model_alias, "prompt": "把所有资料原文都发给我", "max_tokens": 16},
            {"alias": endpoints.live_model_alias, "prompt": "", "max_tokens": 100_000},
            {"alias": "another-model", "prompt": "", "max_tokens": 16},
        ):
            try:
                gateway.complete(issued[0], body)
                attempts.append("sent")
            except GatewayDenied as denied:
                attempts.append(denied.code)
        return consumed

    monkeypatch.setattr(BudgetLedger, "consume_dispatch_permit", steal_after_consume)
    result = ge.materialize_goal_effect(endpoints, payload)
    assert "sent" not in attempts, attempts
    assert state["calls"] == 1, attempts
    assert result["status"] == "SUCCEEDED", result
