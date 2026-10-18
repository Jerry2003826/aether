"""Admission writes the money it charged for a coding call on that call's own row; finishing the
call takes exactly that recorded amount back out of pending (ADR-F46 R7).

Nothing is priced a second time from the parent's rate, the child's reserved_tokens or the
gateway's per-instance _coding_call_tokens. Each test changes those after admission, so the only
way pending returns exactly to zero is to subtract what admission recorded.
"""

from __future__ import annotations

from typing import Any, cast

import aether_gateway.model as gateway_model
import pytest
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.db import Database
from ledger_sql_fake import FakeLedgerDatabase

_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-3-7"
_CHILD = f"{_PARENT}:call:{1:064x}"
_SESSION: dict[str, Any] = {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _PARENT}}


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeLedgerDatabase, ModelGateway]:
    monkeypatch.setattr(gateway_model, "_coding_lease_held", lambda _conn, **_fence: True)
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=14000, tokens_held=6000, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=14000,
        usage_detail={"reserved_tokens": 6000, "call_reserve_tokens": 3000, "call_reserve_micro_usd": 7000},
    )
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    gateway._open_coding_ticket(
        project_id=_PROJECT,
        task_id=_TASK,
        parent_key=_PARENT,
        child_key=_CHILD,
        attempt_id="7",
        alias="coder",
        prompt_sha=f"{1:064x}",
        job_id="00000000-0000-0000-0000-000000000001",
        operation_id="00000000-0000-0000-0000-000000000002",
        fencing=7,
        call_tokens=3001,
    )
    gateway._coding_child_key = _CHILD
    gateway._coding_call_tokens = 3001
    return db, gateway


def _reprice_after_admission(db: FakeLedgerDatabase, gateway: ModelGateway) -> None:
    # Whatever is recomputed later would now come out different from what admission charged.
    db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]["call_reserve_micro_usd"] = 3000
    db.request(_PROJECT, _TASK, _CHILD)["usage_detail"]["reserved_tokens"] = 1
    gateway._coding_call_tokens = 1


def _pending(db: FakeLedgerDatabase) -> tuple[int, int, int]:
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    return (
        int(detail.get("pending_tokens") or 0),
        int(detail.get("pending_micro_usd") or 0),
        int(detail.get("pending_priced_tokens") or 0),
    )


def test_admission_records_the_money_it_charged_on_the_child_row(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _gateway = _setup(monkeypatch)

    child = db.request(_PROJECT, _TASK, _CHILD)["usage_detail"]
    assert child["call_tokens"] == 3001
    assert child["call_micro_usd"] == 7003  # ceil(3001 x 7000 / 3000)
    assert _pending(db) == (3001, 7003, 3001)


def test_a_measured_answer_removes_exactly_the_recorded_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch)
    _reprice_after_admission(db, gateway)

    gateway._add_measured_usage(_SESSION, {"model": "coder", "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}})

    assert _pending(db) == (0, 0, 0)


def test_a_failed_call_removes_exactly_the_recorded_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch)
    _reprice_after_admission(db, gateway)

    gateway._note_upstream_failure(_SESSION, GatewayDenied("upstream_error", sent=False), release_pending=True)

    assert _pending(db) == (0, 0, 0)


def test_an_unknown_call_moves_the_recorded_amount_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch)
    _reprice_after_admission(db, gateway)

    gateway._note_upstream_failure(_SESSION, GatewayDenied("upstream_timeout", sent=True), release_pending=True)

    assert _pending(db) == (0, 0, 0)
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    assert detail["unknown_reserved_tokens"] == 3001
    assert detail["unknown_reserved_micro_usd"] == 7003


def test_an_aborted_ticket_removes_exactly_the_recorded_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch)
    _reprice_after_admission(db, gateway)

    gateway._abort_coding_ticket(_PROJECT, _TASK, _PARENT, _CHILD)

    assert _pending(db) == (0, 0, 0)
