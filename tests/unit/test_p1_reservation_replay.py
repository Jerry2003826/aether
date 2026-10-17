"""A coding reservation the gateway topped up can be replayed by another worker without a dangling hold."""

from __future__ import annotations

from typing import Any, cast

import pytest

from aether_storage.budget import BudgetError, BudgetLedger, apply_reservation_extension
from aether_storage.db import Database
from ledger_sql_fake import FakeLedgerDatabase

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_KEY = "coding-job-1-7"


def _reserve(ledger: BudgetLedger, *, amount: int = 4096, tokens: int = 4096) -> bool:
    return ledger.reserve(
        owner_id=_OWNER,
        project_id=_PROJECT,
        task_id=_TASK,
        request_key=_KEY,
        amount=amount,
        role="coding",
        tokens=tokens,
        extra_detail={"call_reserve_tokens": 4096, "call_reserve_micro_usd": 4096},
    )


def _top_up(db: FakeLedgerDatabase, *, amount: int, tokens: int) -> None:
    row = db.request(_PROJECT, _TASK, _KEY)
    detail: dict[str, Any] = dict(row["usage_detail"])
    apply_reservation_extension(
        db,
        account_id=db.account(_PROJECT, _TASK)["id"],
        project_id=_PROJECT,
        task_id=_TASK,
        request_key=_KEY,
        amount=amount,
        tokens=tokens,
        detail=detail,
    )


def _ledger() -> tuple[FakeLedgerDatabase, BudgetLedger]:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK)
    return db, BudgetLedger(cast(Database, db))


def test_replaying_a_topped_up_reservation_is_idempotent_and_adds_no_hold() -> None:
    db, ledger = _ledger()
    assert _reserve(ledger) is True
    _top_up(db, amount=2800, tokens=2800)
    before = dict(db.account(_PROJECT, _TASK))

    assert _reserve(ledger) is True

    after = db.account(_PROJECT, _TASK)
    assert after["held_micro_usd"] == before["held_micro_usd"] == 4096 + 2800
    assert after["tokens_held"] == before["tokens_held"] == 4096 + 2800
    assert after["requests_admitted"] == before["requests_admitted"] == 1
    row = db.request(_PROJECT, _TASK, _KEY)
    assert row["reserved_micro_usd"] == 4096 + 2800
    assert row["usage_detail"]["reserved_tokens"] == 4096 + 2800


def test_replaying_after_several_top_ups_still_matches() -> None:
    db, ledger = _ledger()
    assert _reserve(ledger) is True
    _top_up(db, amount=2800, tokens=2800)
    _top_up(db, amount=1500, tokens=1500)

    assert _reserve(ledger) is True
    assert db.account(_PROJECT, _TASK)["held_micro_usd"] == 4096 + 2800 + 1500


def test_a_replay_with_a_different_amount_is_still_a_mismatch_after_a_top_up() -> None:
    db, ledger = _ledger()
    assert _reserve(ledger) is True
    _top_up(db, amount=2800, tokens=2800)

    with pytest.raises(BudgetError) as caught:
        _reserve(ledger, amount=4096 + 2800, tokens=4096 + 2800)
    assert caught.value.code == "idempotency_mismatch"
    assert db.account(_PROJECT, _TASK)["held_micro_usd"] == 4096 + 2800


def test_a_replay_without_a_top_up_still_matches_and_a_tampered_amount_does_not() -> None:
    db, ledger = _ledger()
    assert _reserve(ledger) is True
    assert _reserve(ledger) is True
    db.request(_PROJECT, _TASK, _KEY)["reserved_micro_usd"] = 5000
    with pytest.raises(BudgetError) as caught:
        _reserve(ledger)
    assert caught.value.code == "idempotency_mismatch"
