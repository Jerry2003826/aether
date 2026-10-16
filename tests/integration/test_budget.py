import threading

import pytest
from sqlalchemy import text

from aether_storage.budget import BudgetError, BudgetLedger
from aether_storage.db import Database
from tests.integration.conftest import insert_task
from tests.integration.test_auth import _bootstrap


def _open(client, api_db, role_urls, key: str, *, limit=100, tokens=100, requests=10, fraction=0.2):
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": f"budget-{key}"},
        json={"name": key},
    )
    assert project.status_code == 201, project.text
    task_id = insert_task(api_db, session["owner_id"], project.json()["id"], key)
    ledger = BudgetLedger(Database(role_urls["aether_worker"]))
    ledger.open_account(
        owner_id=session["owner_id"],
        project_id=project.json()["id"],
        task_id=task_id,
        limit_micro_usd=limit,
        token_limit=tokens,
        max_requests=requests,
        verification_reserve_fraction=fraction,
    )
    return session["owner_id"], project.json()["id"], task_id, ledger


def test_budget_overrun_is_rejected(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "overrun", fraction=0)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="one", amount=70, role="actor") is True
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="two", amount=40, role="actor") is False
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["held_micro_usd"] == 70


def test_concurrent_reservation_is_atomic(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "race", fraction=0)
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt(key: str) -> None:
        barrier.wait()
        results.append(ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=key, amount=60, role="actor"))

    threads = [threading.Thread(target=attempt, args=("a",)), threading.Thread(target=attempt, args=("b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["held_micro_usd"] == 60


def test_duplicate_settlement_does_not_double_charge(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "settle", fraction=0)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="once", amount=30, role="actor") is True
    ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="once", actual=30, input_tokens=1, output_tokens=1)
    ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="once", actual=30, input_tokens=1, output_tokens=1)
    with pytest.raises(BudgetError) as conflict:
        ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="once", actual=99, input_tokens=1, output_tokens=1)
    assert conflict.value.code == "conflicting_settlement"
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["actual_known_micro_usd"] == 30


def test_timeout_keeps_reservation_held(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "held", fraction=0)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="slow", amount=25, role="actor") is True
    ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="slow")
    snapshot = ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)
    assert snapshot["held_micro_usd"] == 25
    assert snapshot["actual_known_micro_usd"] == 0
    assert snapshot["unknown_request_count"] == 1


def test_unknown_usage_is_not_stored_as_zero(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "unknown", fraction=0)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="priced", amount=10, role="actor") is True
    ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="priced")
    with pytest.raises(BudgetError) as rejected:
        ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="priced", actual=0, input_tokens=0, output_tokens=0)
    assert rejected.value.code == "unknown_not_zero"
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        actual = conn.execute(text("SELECT actual_micro_usd FROM model_requests WHERE request_key = 'priced'")).scalar_one()
    assert actual is None


def test_internal_roles_share_one_ledger(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "roles", limit=1000, fraction=0)
    for role in ("planner", "repair", "judge", "coder"):
        assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=role, amount=10, role=role) is True
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        roles = {row[0] for row in conn.execute(text("SELECT role FROM model_requests WHERE task_id = :task_id"), {"task_id": task_id})}
    assert roles == {"planner", "repair", "judge", "coder"}
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["held_micro_usd"] == 40


def test_same_key_different_request_is_rejected(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "hash", fraction=0)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="same", amount=10, role="actor", request={"prompt": "one"}) is True
    with pytest.raises(BudgetError) as mismatch:
        ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="same", amount=10, role="actor", request={"prompt": "two"})
    assert mismatch.value.code == "idempotency_mismatch"


def test_concurrent_token_reservations_stay_within_limit(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "tokens", limit=1000, tokens=10, fraction=0)
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt(key: str) -> None:
        barrier.wait()
        results.append(ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=key, amount=1, role="actor", tokens=6))

    threads = [threading.Thread(target=attempt, args=("t1",)), threading.Thread(target=attempt, args=("t2",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["tokens_held"] == 6


def test_settle_rejects_actual_above_reservation(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "cap", fraction=0)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="cap", amount=15, role="actor") is True
    with pytest.raises(BudgetError) as rejected:
        ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="cap", actual=16, input_tokens=1, output_tokens=1)
    assert rejected.value.code == "actual_exceeds_reservation"
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["actual_known_micro_usd"] == 0
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["held_micro_usd"] == 15


def test_measured_overrun_is_charged_and_the_hold_is_released(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "measured-over", limit=1000, tokens=1000, fraction=0)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="over", amount=100, role="coding", tokens=100) is True
    assert ledger.settle_measured(
        owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="over", actual=250, input_tokens=200, output_tokens=50
    ) == "overrun"
    assert ledger.settle_measured(
        owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="over", actual=250, input_tokens=200, output_tokens=50
    ) == "overrun"
    snapshot = ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)
    assert snapshot["held_micro_usd"] == 0
    assert snapshot["actual_known_micro_usd"] == 250
    assert snapshot["tokens_held"] == 0
    assert snapshot["tokens_used"] == 250
    assert snapshot["unknown_request_count"] == 0
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT usage_status, actual_micro_usd, input_tokens, output_tokens, usage_detail FROM model_requests WHERE request_key = 'over'")
        ).mappings().one()
    assert row["usage_status"] == "known"
    assert int(row["actual_micro_usd"]) == 250
    assert int(row["input_tokens"]) == 200
    assert int(row["output_tokens"]) == 50
    detail = row["usage_detail"]
    assert detail["book_result"] == "overrun"
    assert detail["overrun_micro_usd"] == 150
    assert detail["overrun_tokens"] == 150
    assert detail["reconciliation"] == "recorded"
    ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="over", actual=250, input_tokens=200, output_tokens=50)
    with pytest.raises(BudgetError) as rejected:
        ledger.settle(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="over", actual=251, input_tokens=201, output_tokens=50)
    assert rejected.value.code == "conflicting_settlement"
    assert ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["actual_known_micro_usd"] == 250


def test_verification_reserve_is_protected(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, ledger = _open(client, api_db, role_urls, "reserve", limit=100, fraction=0.2)
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner", amount=80, role="planner") is True
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="planner-more", amount=1, role="planner") is False
    assert ledger.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key="judge", amount=20, role="judge", verification=True) is True
