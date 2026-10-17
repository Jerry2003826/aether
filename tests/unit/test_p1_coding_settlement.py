"""Coding reservations: a call whose usage is unknown is charged at its reservation, not released."""

from __future__ import annotations

from typing import Any, cast

from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_jobs import _settle
from ledger_sql_fake import FakeLedgerDatabase

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-1"
_CALL = 3000


def _setup(*, reserved: int, pending: int) -> FakeLedgerDatabase:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=reserved, tokens_held=reserved, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=reserved,
        usage_detail={
            "reserved_tokens": reserved,
            "call_reserve_tokens": 4096,
            "call_reserve_micro_usd": 4096,
            "pending_tokens": pending,
        },
    )
    return db


def _gateway(db: FakeLedgerDatabase) -> ModelGateway:
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    gateway._coding_call_tokens = _CALL
    return gateway


def _session() -> dict[str, Any]:
    return {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _PARENT}}


def _answered(gateway: ModelGateway, usage: dict[str, int] | None) -> None:
    result: dict[str, Any] = {"model": "coder", "choices": []}
    if usage is not None:
        result["usage"] = usage
    gateway._add_measured_usage(_session(), result)


def _timed_out(gateway: ModelGateway) -> None:
    gateway._note_upstream_failure(_session(), GatewayDenied("upstream_error", sent=True), release_pending=True)


def _settle_job(db: FakeLedgerDatabase) -> None:
    _settle(
        cast(BudgetLedger, BudgetLedger(cast(Database, db))),
        owner_id=_OWNER,
        project_id=_PROJECT,
        task_id=_TASK,
        request_key=_PARENT,
        reserved=True,
        result=None,
    )


def test_a_call_that_timed_out_after_measured_calls_is_charged_at_its_reservation() -> None:
    db = _setup(reserved=8192, pending=2 * _CALL)
    gateway = _gateway(db)
    _answered(gateway, {"prompt_tokens": 100, "completion_tokens": 50})
    _timed_out(gateway)

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    row = db.request(_PROJECT, _TASK, _PARENT)
    assert row["usage_status"] == "known"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["tokens_used"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0
    assert row["actual_micro_usd"] == 150 + _CALL
    assert row["usage_detail"]["unknown_usage_calls"] == 1


def test_a_call_without_usage_stays_charged_even_when_a_later_call_reports_usage() -> None:
    db = _setup(reserved=12288, pending=3 * _CALL)
    gateway = _gateway(db)
    _answered(gateway, {"prompt_tokens": 100, "completion_tokens": 50})
    _answered(gateway, None)
    _answered(gateway, {"prompt_tokens": 150, "completion_tokens": 50})

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    row = db.request(_PROJECT, _TASK, _PARENT)
    assert row["usage_status"] == "known"
    assert (row["input_tokens"], row["output_tokens"]) == (250, 100)
    assert account["spent_micro_usd"] == 350 + _CALL
    assert account["tokens_used"] == 350 + _CALL
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_the_unknown_charge_never_exceeds_what_the_reservation_held() -> None:
    db = _setup(reserved=4096, pending=2 * _CALL)
    gateway = _gateway(db)
    _answered(gateway, {"prompt_tokens": 2000, "completion_tokens": 1000})
    _timed_out(gateway)

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    assert account["spent_micro_usd"] == 4096
    assert account["tokens_used"] == 4096
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_a_legacy_row_flagged_usage_missing_without_a_recorded_amount_keeps_the_whole_hold_charged() -> None:
    db = _setup(reserved=4096, pending=0)
    row = db.request(_PROJECT, _TASK, _PARENT)
    row["usage_detail"].update({"measured_input_tokens": 100, "measured_output_tokens": 50, "usage_missing": True})

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    assert account["spent_micro_usd"] == 4096
    assert account["tokens_used"] == 4096
    assert account["held_micro_usd"] == 0


def test_fully_measured_calls_still_settle_at_measured_usage() -> None:
    db = _setup(reserved=8192, pending=2 * _CALL)
    gateway = _gateway(db)
    _answered(gateway, {"prompt_tokens": 100, "completion_tokens": 50})
    _answered(gateway, {"prompt_tokens": 200, "completion_tokens": 25})

    _settle_job(db)

    account = db.account(_PROJECT, _TASK)
    assert account["spent_micro_usd"] == 375
    assert account["tokens_used"] == 375
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0
