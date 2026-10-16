"""P2-05 · 模型令牌不绑任务、不经账本、任务结束后仍可用。

不变量 INV-AUTH-03：模型令牌只服务于一个任务的已记账调用，任务结束或取消后失效；
不变量 INV-MONEY-05：经过网关的每次供应商调用都进预算账本。
目前令牌只在 worker 进程内存里，没有交给沙箱，所以这是潜在面；runner 一旦接上真实网关就变成可利用的面。
"""

from __future__ import annotations

import aether_worker.goal_effect as ge
from aether_gateway.model import ModelGateway
from audit_helpers import rows
from tests.integration.test_audit_round4 import setup_case


def test_model_token_dies_with_its_task_and_cannot_bypass_the_ledger(client, role_urls, monkeypatch) -> None:
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    issued: list[str] = []
    original = ModelGateway.issue_token

    def capture(self, **kwargs):
        token = original(self, **kwargs)
        issued.append(token)
        return token

    monkeypatch.setattr(ModelGateway, "issue_token", capture)
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
