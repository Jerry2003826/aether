"""A call the job stopped waiting for is charged once, at its reservation, even if it answers later."""

from __future__ import annotations

from typing import Any, cast

import pytest
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_jobs import _drain_proxy, _settle
from aether_worker.coding_model_proxy import CodingModelProxy
from ledger_sql_fake import FakeLedgerDatabase, prices_at

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-1"
_CALL = 3000


class _StuckProxy:
    def drain(self, *, timeout: float | None = None) -> int:
        return 1


def _setup() -> FakeLedgerDatabase:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=8192, tokens_held=8192, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=8192,
        usage_detail={
            "reserved_tokens": 8192,
            "call_reserve_tokens": 4096,
            "call_reserve_micro_usd": 4096,
            **prices_at(4096, 4096),
            "pending_tokens": 2 * _CALL,
        },
    )
    return db


def _gateway(db: FakeLedgerDatabase) -> ModelGateway:
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    gateway._coding_call_tokens = _CALL
    return gateway


def _session() -> dict[str, Any]:
    return {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _PARENT}}


def _drain_then_settle(db: FakeLedgerDatabase, late: Any) -> None:
    ledger = BudgetLedger(cast(Database, db))
    _drain_proxy(
        cast(CodingModelProxy, _StuckProxy()),
        ledger,
        owner_id=_OWNER,
        project_id=_PROJECT,
        task_id=_TASK,
        request_key=_PARENT,
    )
    late()
    _settle(ledger, owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None)


@pytest.mark.parametrize("outcome", ["answered", "failed"])
def test_a_call_answering_after_the_drain_gave_up_is_not_charged_twice(outcome: str) -> None:
    db = _setup()
    gateway = _gateway(db)
    gateway._add_measured_usage(_session(), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})

    def late() -> None:
        if outcome == "answered":
            result = {"model": "coder", "choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 100}}
            gateway._add_measured_usage(_session(), result)
        else:
            gateway._note_upstream_failure(_session(), GatewayDenied("upstream_error", sent=True), release_pending=True)

    _drain_then_settle(db, late)

    account = db.account(_PROJECT, _TASK)
    row = db.request(_PROJECT, _TASK, _PARENT)
    assert row["usage_status"] == "known"
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)
    assert row["usage_detail"]["unknown_usage_calls"] == 1
    assert row["usage_detail"]["unknown_reserved_tokens"] == _CALL
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["tokens_used"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0
