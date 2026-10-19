"""Admitting the next coding call reserves and checks money, with the per-call round-up, not only tokens.

Each call is charged ceil(tokens x rate) on its own (ADR-F46 R7). If admission grows the hold in
tokens and prices only the token gap, the per-call amounts can add up to more money than is held,
and an ordinary job is booked as an overrun. Rate 7000 micro-USD per 3000 tokens: a 1-token call
costs 3 and a 3001-token call 7003, together 7006, while a token-only top-up held 7000 + 5 = 7005.
"""

from __future__ import annotations

from typing import Any, cast

import aether_gateway.model as gateway_model
import pytest
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_jobs import _settle
from ledger_sql_fake import FakeLedgerDatabase, prices_at

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-2-7"
_TOKENS = 3000
_MICRO = 7000


def _setup(monkeypatch: pytest.MonkeyPatch, **account: Any) -> tuple[FakeLedgerDatabase, ModelGateway]:
    monkeypatch.setattr(gateway_model, "_coding_lease_held", lambda _conn, **_fence: True)
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=_MICRO, tokens_held=_TOKENS, requests_admitted=1, **account)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=_MICRO,
        usage_detail={"reserved_tokens": _TOKENS, "call_reserve_tokens": _TOKENS, "call_reserve_micro_usd": _MICRO, **prices_at(_MICRO, _TOKENS)},
    )
    return db, ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")


def _open(gateway: ModelGateway, index: int, call_tokens: int) -> None:
    gateway._open_coding_ticket(
        project_id=_PROJECT,
        task_id=_TASK,
        parent_key=_PARENT,
        child_key=f"{_PARENT}:call:{index:064x}",
        attempt_id="7",
        alias="coder",
        prompt_sha=f"{index:064x}",
        job_id="00000000-0000-0000-0000-000000000001",
        operation_id="00000000-0000-0000-0000-000000000002",
        fencing=7,
        call_tokens=call_tokens,
    )
    gateway._coding_call_tokens = call_tokens
    gateway._coding_child_key = f"{_PARENT}:call:{index:064x}"


def _measured(gateway: ModelGateway, incoming: int, outgoing: int) -> None:
    session = {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _PARENT}}
    gateway._add_measured_usage(session, {"model": "coder", "choices": [], "usage": {"prompt_tokens": incoming, "completion_tokens": outgoing}})


def test_ordinary_calls_at_a_fractional_rate_are_never_booked_as_an_overrun(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch)
    _open(gateway, 1, 1)
    _measured(gateway, 1, 0)
    _open(gateway, 2, 3001)

    parent = db.request(_PROJECT, _TASK, _PARENT)
    # Held money covers the measured call (3) plus the next call priced on its own (7003).
    assert parent["reserved_micro_usd"] >= 3 + 7003
    assert db.account(_PROJECT, _TASK)["held_micro_usd"] == parent["reserved_micro_usd"]

    _measured(gateway, 3000, 1)
    _settle(BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None)

    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    assert row["usage_detail"].get("book_result") == "settled"
    assert "overrun_micro_usd" not in row["usage_detail"]
    assert account["spent_micro_usd"] == 3 + 7003
    assert account["tokens_used"] == 1 + 3001
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_an_unknown_call_is_held_at_its_own_rounded_price(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch)
    _open(gateway, 1, 1)
    _measured(gateway, 1, 0)
    _open(gateway, 2, 3001)
    session = {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _PARENT}}
    gateway._note_upstream_failure(session, GatewayDenied("upstream_timeout", sent=True), release_pending=True)
    _settle(BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None)

    row = db.request(_PROJECT, _TASK, _PARENT)
    assert row["usage_detail"].get("book_result") == "settled"
    assert db.account(_PROJECT, _TASK)["spent_micro_usd"] == 3 + 7003


def test_admission_refuses_a_call_whose_rounded_price_does_not_fit_the_money_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    # Money ceiling int(8757 * 0.8) = 7005: enough for a token-only top-up (7000 + 5), not for 3 + 7003.
    db, gateway = _setup(monkeypatch, limit_micro_usd=8757)
    _open(gateway, 1, 1)
    _measured(gateway, 1, 0)

    with pytest.raises(GatewayDenied) as caught:
        _open(gateway, 2, 3001)

    assert caught.value.code == "budget_exceeded"
    assert db.request(_PROJECT, _TASK, _PARENT)["reserved_micro_usd"] == _MICRO
    assert db.account(_PROJECT, _TASK)["held_micro_usd"] == _MICRO
