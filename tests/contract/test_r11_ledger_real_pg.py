"""The budget ledger against a real PostgreSQL: the model_requests unique key, the FOR UPDATE lock that
serialises concurrent reservations, and row-level security between projects (r11 plan c1)."""

from __future__ import annotations

import threading
import time
import uuid

import psycopg
import pytest
from aether_storage.budget import BudgetError, BudgetLedger
from sqlalchemy import text
from sqlalchemy.exc import (
    IntegrityError,
    NoResultFound,
    ProgrammingError,
    SQLAlchemyError,
)


def test_ledger_unique_constraint_on_real_pg(new_scope, worker_db) -> None:
    scope = new_scope()
    ledger = BudgetLedger(worker_db)
    ledger.open_account(owner_id=scope.owner_id, project_id=scope.project_id, task_id=scope.task_id, limit_micro_usd=10_000, token_limit=10_000, max_requests=10)
    keys = {"owner_id": scope.owner_id, "project_id": scope.project_id, "task_id": scope.task_id, "request_key": "call-1", "role": "researcher"}
    assert ledger.reserve(**keys, amount=100) is True
    # The same request replays; a different request under the same key is refused, never a second row.
    assert ledger.reserve(**keys, amount=100) is True
    with pytest.raises(BudgetError, match="idempotency_mismatch"):
        ledger.reserve(**keys, amount=200)
    # The constraint itself, below the ledger: UNIQUE(project_id, task_id, request_key).
    with pytest.raises(IntegrityError) as failure, worker_db.transaction(owner_id=scope.owner_id, project_id=scope.project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status) "
                "VALUES (:p, :t, 'call-1', repeat('0', 64), 'researcher', 'researcher', 1, 'reserved')"
            ),
            {"p": scope.project_id, "t": scope.task_id},
        )
    assert isinstance(failure.value.orig, psycopg.errors.UniqueViolation), failure.value
    with worker_db.transaction(owner_id=scope.owner_id, project_id=scope.project_id) as conn:
        rows = conn.execute(text("SELECT count(*) FROM model_requests WHERE task_id = :t"), {"t": scope.task_id}).scalar_one()
        held = conn.execute(text("SELECT held_micro_usd, requests_admitted FROM budget_accounts WHERE task_id = :t"), {"t": scope.task_id}).one()
    assert rows == 1
    assert tuple(held) == (100, 1)


def _race_reservations(ledger: BudgetLedger, scope, *, parties: int = 12, started: int = 12) -> tuple[list[bool], list[Exception], list[threading.Thread]]:
    """Start `started` threads that meet at a barrier for `parties`, then each reserve 100 under its own key."""
    start = threading.Barrier(parties, timeout=30)
    outcomes: list[bool] = []
    errors: list[Exception] = []

    def reserve(index: int) -> None:
        try:
            start.wait()
            outcomes.append(
                ledger.reserve(owner_id=scope.owner_id, project_id=scope.project_id, task_id=scope.task_id, request_key=f"call-{index}", amount=100, role="researcher")
            )
        except (threading.BrokenBarrierError, BudgetError, SQLAlchemyError) as exc:  # reported below, with every other outcome
            errors.append(exc)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(started)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes, errors, [thread for thread in threads if thread.is_alive()]


def _three_reservation_account(new_scope, worker_db):
    # Room for exactly three reservations of 100 under the 80% non-verification ceiling (375 * 0.8 = 300).
    scope = new_scope()
    ledger = BudgetLedger(worker_db)
    ledger.open_account(owner_id=scope.owner_id, project_id=scope.project_id, task_id=scope.task_id, limit_micro_usd=375, token_limit=10_000, max_requests=100)
    return scope, ledger


def test_concurrent_reservation_lock(new_scope, worker_db) -> None:
    scope, ledger = _three_reservation_account(new_scope, worker_db)
    outcomes, errors, stuck = _race_reservations(ledger, scope)
    assert stuck == [], stuck
    assert errors == [], errors
    assert sorted(outcomes) == [False] * 9 + [True] * 3, outcomes
    with worker_db.transaction(owner_id=scope.owner_id, project_id=scope.project_id) as conn:
        held = conn.execute(text("SELECT held_micro_usd, requests_admitted FROM budget_accounts WHERE task_id = :t"), {"t": scope.task_id}).one()
        rows = conn.execute(text("SELECT count(*) FROM model_requests WHERE task_id = :t"), {"t": scope.task_id}).scalar_one()
    assert tuple(held) == (300, 3)
    assert rows == 3


def test_concurrent_reservation_reports_a_party_that_never_arrives(new_scope, worker_db) -> None:
    # One of the twelve threads never starts: the barrier must break and every waiter must say so,
    # instead of the race hanging until join gives up and leaving nothing to read.
    scope, ledger = _three_reservation_account(new_scope, worker_db)
    begun = time.monotonic()
    outcomes, errors, stuck = _race_reservations(ledger, scope, started=11)
    assert stuck == [], f"{len(stuck)} threads still waiting at the barrier after {time.monotonic() - begun:.0f}s"
    assert outcomes == []
    assert len(errors) == 11, errors
    assert all(isinstance(error, threading.BrokenBarrierError) for error in errors), errors
    with worker_db.transaction(owner_id=scope.owner_id, project_id=scope.project_id) as conn:
        rows = conn.execute(text("SELECT count(*) FROM model_requests WHERE task_id = :t"), {"t": scope.task_id}).scalar_one()
    assert rows == 0


def test_rls_denies_cross_project(new_scope, worker_db) -> None:
    mine = new_scope()
    theirs = new_scope()
    ledger = BudgetLedger(worker_db)
    ledger.open_account(owner_id=theirs.owner_id, project_id=theirs.project_id, task_id=theirs.task_id, limit_micro_usd=10_000, token_limit=10_000, max_requests=10)
    assert ledger.reserve(owner_id=theirs.owner_id, project_id=theirs.project_id, task_id=theirs.task_id, request_key="theirs", amount=10, role="researcher")
    with worker_db.transaction(owner_id=mine.owner_id, project_id=mine.project_id) as conn:
        for table in ("tasks", "budget_accounts", "model_requests"):
            seen = conn.execute(text(f"SELECT count(*) FROM {table} WHERE project_id = :p"), {"p": theirs.project_id}).scalar_one()
            assert seen == 0, table
    # projects is scoped by owner: another owner sees neither project.
    with worker_db.transaction(owner_id=str(uuid.uuid4()), project_id=mine.project_id) as conn:
        seen = conn.execute(text("SELECT count(*) FROM projects WHERE id IN (:a, :b)"), {"a": mine.project_id, "b": theirs.project_id}).scalar_one()
    assert seen == 0
    # Writing into the other project from this project's scope is refused by the policy's WITH CHECK.
    with pytest.raises(ProgrammingError) as failure, worker_db.transaction(owner_id=mine.owner_id, project_id=mine.project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status) "
                "VALUES (:p, :t, 'smuggled', repeat('0', 64), 'researcher', 'researcher', 1, 'reserved')"
            ),
            {"p": theirs.project_id, "t": theirs.task_id},
        )
    assert isinstance(failure.value.orig, psycopg.errors.InsufficientPrivilege), failure.value
    # And the ledger itself, scoped to this project, finds no account for the other project's task.
    with pytest.raises(NoResultFound):
        ledger.reserve(owner_id=mine.owner_id, project_id=mine.project_id, task_id=theirs.task_id, request_key="cross", amount=10, role="researcher")
