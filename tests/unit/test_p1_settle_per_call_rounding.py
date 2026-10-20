"""Money is an integer number of micro-USD: each coding call is rounded up on its own, then summed.

A call of t tokens costs ceil(t * call_reserve_micro_usd / call_reserve_tokens). Measured calls use
their own measured tokens, unknown calls their own reservation. The final settlement adds the
per-call amounts; the cap on what is still held and the overrun check apply once, to that sum.
Rounding the summed tokens once instead under-charges by up to one micro-USD per call.
"""

from __future__ import annotations

from typing import Any, cast

from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_jobs import _settle
from ledger_sql_fake import FakeLedgerDatabase, prices_at

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-rounding"


def _setup(*, rate_tokens: int, rate_micro: int, reserved_tokens: int | None = None, reserved_micro: int | None = None) -> FakeLedgerDatabase:
    tokens = rate_tokens if reserved_tokens is None else reserved_tokens
    micro = rate_micro if reserved_micro is None else reserved_micro
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=micro, tokens_held=tokens, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=micro,
        usage_detail={"reserved_tokens": tokens, "call_reserve_tokens": rate_tokens, "call_reserve_micro_usd": rate_micro, **prices_at(rate_micro, rate_tokens)},
    )
    return db


def _session() -> dict[str, Any]:
    return {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _PARENT}}


def _gateway(db: FakeLedgerDatabase, call_tokens: int) -> ModelGateway:
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    gateway._coding_call_tokens = call_tokens
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    detail["pending_tokens"] = int(detail.get("pending_tokens") or 0) + call_tokens
    return gateway


def _measured(db: FakeLedgerDatabase, incoming: int, outgoing: int) -> None:
    _gateway(db, incoming + outgoing)._add_measured_usage(
        _session(), {"model": "coder", "choices": [], "usage": {"prompt_tokens": incoming, "completion_tokens": outgoing}}
    )


def _unknown(db: FakeLedgerDatabase, call_tokens: int) -> None:
    _gateway(db, call_tokens)._note_upstream_failure(_session(), GatewayDenied("upstream_error", sent=True), release_pending=True)


def _settle_job(db: FakeLedgerDatabase) -> None:
    _settle(
        BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None
    )


def test_each_call_is_rounded_up_before_the_final_sum() -> None:
    # r = 7000 / 3000 = 7/3. One 4-token call costs ceil(28 / 3) = 10.
    db = _setup(rate_tokens=3000, rate_micro=7000)
    _measured(db, 2, 2)
    _measured(db, 2, 2)

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    row = db.request(_PROJECT, _TASK, _PARENT)
    assert account["spent_micro_usd"] == 20  # not 19 = ceil(56 / 3) for the sum, not 8 = tokens at 1:1
    assert row["actual_micro_usd"] == 20
    assert account["tokens_used"] == 8
    assert (row["input_tokens"], row["output_tokens"]) == (4, 4)
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_unknown_calls_are_rounded_up_one_by_one() -> None:
    # Settlement needs some measured usage; one 3-token call costs ceil(21 / 3) = 7.
    db = _setup(rate_tokens=3000, rate_micro=7000)
    _measured(db, 2, 1)
    _unknown(db, 1000)
    _unknown(db, 1000)

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    # Each unknown call: ceil(7000 / 3) = 2334. Rounding the 2000 tokens once would give 4667.
    assert detail["unknown_charged_micro_usd"] == 2 * 2334
    assert detail["unknown_charged_tokens"] == 2000
    assert account["spent_micro_usd"] == 7 + 2 * 2334
    assert account["tokens_used"] == 3 + 2000


def test_measured_and_unknown_calls_are_rounded_per_call_and_summed() -> None:
    db = _setup(rate_tokens=3000, rate_micro=7000)
    _measured(db, 1, 1)
    _unknown(db, 1000)
    _measured(db, 1, 1)

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    row = db.request(_PROJECT, _TASK, _PARENT)
    # Two measured 2-token calls at ceil(14 / 3) = 5 each, one unknown 1000-token call at 2334.
    assert row["actual_micro_usd"] == 5 + 5 + 2334
    assert account["spent_micro_usd"] == 5 + 5 + 2334
    assert row["usage_detail"]["unknown_charged_micro_usd"] == 2334
    assert account["tokens_used"] == 4 + 1000
    assert row["usage_detail"]["book_result"] == "settled"


def test_the_money_cap_applies_to_the_sum_not_to_each_call() -> None:
    # Three 1000-token calls at 7/3: each costs 2334, which fits the 7000 held on its own; the sum, 7002, does not.
    db = _setup(rate_tokens=3000, rate_micro=7000)
    _measured(db, 500, 500)
    _measured(db, 500, 500)
    _measured(db, 500, 500)
    _unknown(db, 500)

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    row = db.request(_PROJECT, _TASK, _PARENT)
    detail = row["usage_detail"]
    assert detail["book_result"] == "overrun"
    assert detail["overrun_micro_usd"] == 3 * 2334 - 7000
    assert detail["overrun_tokens"] == 0
    # Nothing is left of either hold for the unknown call once the measured sum is booked.
    assert detail["unknown_charged_micro_usd"] == 0
    assert detail["unknown_charged_tokens"] == 0
    assert row["actual_micro_usd"] == 3 * 2334
    assert account["spent_micro_usd"] == 3 * 2334
    assert account["tokens_used"] == 3000
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_an_integer_rate_is_unchanged_by_per_call_rounding() -> None:
    for factor in (1, 2):
        db = _setup(rate_tokens=3000, rate_micro=3000 * factor)
        _measured(db, 2, 2)
        _measured(db, 2, 2)
        _unknown(db, 1000)

        _settle_job(db)

        account = db.account(_PROJECT, _TASK)
        assert account["spent_micro_usd"] == (8 + 1000) * factor
        assert account["tokens_used"] == 8 + 1000
        assert account["held_micro_usd"] == 0
        assert account["tokens_held"] == 0
