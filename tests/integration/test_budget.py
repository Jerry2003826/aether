import threading

import pytest

from aether_storage.budget import BudgetError, BudgetLedger
from tests.integration.conftest import insert_task
from tests.integration.test_auth import _bootstrap


def _session(client):
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    return session


def _project(client, session, key: str, name: str, api_db, goal: str):
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": key},
        json={"name": name},
    )
    assert project.status_code == 201, project.text
    task_id = insert_task(api_db, session["owner_id"], project.json()["id"], goal)
    return session["owner_id"], project.json()["id"], task_id


def test_reserve_settle_unknown_and_concurrency(client, api_db) -> None:
    session = _session(client)
    owner_id, project_id, task_id = _project(client, session, "budget-project", "预算", api_db, "预算任务")
    ledger = BudgetLedger(api_db)
    ledger.open_account(owner_id=owner_id, project_id=project_id, task_id=task_id, limit_micro_usd=100, token_limit=100, max_requests=10, verification_reserve_fraction=0.2)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-1", amount=80, role="planner") is True
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-1", amount=80, role="planner") is True
    with pytest.raises(BudgetError):
        ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-1", amount=10, role="planner")
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-2", amount=1, role="planner") is False
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="judge-1", amount=20, role="judge", verification=True) is True
    ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-1", actual=90, input_tokens=2, output_tokens=1)
    ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-1", actual=90, input_tokens=2, output_tokens=1)
    with pytest.raises(BudgetError):
        ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-1", actual=1, input_tokens=1, output_tokens=1)
    ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="judge-1")
    with pytest.raises(BudgetError):
        ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="judge-1", actual=0, input_tokens=0, output_tokens=0)
    snapshot = ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)
    assert snapshot["actual_known_micro_usd"] == 90
    assert snapshot["held_micro_usd"] == 20
    assert snapshot["unknown_request_count"] == 1

    owner_id, project_id, task_id = _project(client, session, "budget-race", "竞争", api_db, "竞争任务")
    race = BudgetLedger(api_db)
    race.open_account(owner_id=owner_id, project_id=project_id, task_id=task_id, limit_micro_usd=100, token_limit=100, max_requests=10, verification_reserve_fraction=0)
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt(key: str) -> None:
        barrier.wait()
        results.append(race.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=key, amount=60, role="actor"))

    threads = [threading.Thread(target=attempt, args=("a",)), threading.Thread(target=attempt, args=("b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
    assert race.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["held_micro_usd"] == 60
