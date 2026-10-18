"""A call whose usage is unknown keeps its reservation occupied when the next call is admitted.

A sent call that came back without usage (a timeout, an answer without usage, a cut reply) moves its
allowance from pending_tokens to unknown_reserved_tokens and is charged that at settlement. The next
call's admission must count it as occupied next to the measured and pending tokens. Otherwise the
next call reuses the same hold, and when that call reports usage the settlement caps the unknown
charge at what is left: part of a possibly billed call ends up neither spent nor held.
"""

from __future__ import annotations

from typing import Any, cast

import aether_gateway.model as gateway_model
import pytest
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_jobs import _settle
from ledger_sql_fake import FakeLedgerDatabase

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-1-7"


def _setup(monkeypatch: pytest.MonkeyPatch, *, reserved: int, detail: dict[str, Any]) -> tuple[FakeLedgerDatabase, ModelGateway]:
    monkeypatch.setattr(gateway_model, "_coding_lease_held", lambda _conn, **_fence: True)
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=reserved, tokens_held=reserved, requests_admitted=1)
    db.add_request(_PROJECT, _TASK, _PARENT, reserved_micro_usd=reserved, usage_detail={"reserved_tokens": reserved, **detail})
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


def _session() -> dict[str, Any]:
    return {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _PARENT}}


def test_the_next_call_is_admitted_on_top_of_an_unknown_call_not_inside_its_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch, reserved=4096, detail={"call_reserve_tokens": 4096, "call_reserve_micro_usd": 4096})
    _open(gateway, 1, 1000)
    gateway._note_upstream_failure(_session(), GatewayDenied("upstream_error", sent=True), release_pending=True)
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    assert (detail["pending_tokens"], detail["unknown_reserved_tokens"]) == (0, 1000)

    _open(gateway, 2, 1000)

    parent = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    # Occupied: 1000 unknown + 1000 pending; the next call still needs a whole call allowance (4096).
    assert parent["usage_detail"]["reserved_tokens"] == 1000 + 4096
    assert parent["reserved_micro_usd"] == 1000 + 4096
    assert account["tokens_held"] == 1000 + 4096
    assert account["held_micro_usd"] == 1000 + 4096

    gateway._add_measured_usage(_session(), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 3500, "completion_tokens": 500}})
    _settle(BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None)

    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    # The unknown call is charged in full at its reservation, not only the 96 tokens left beside the second call.
    assert row["usage_detail"]["unknown_charged_tokens"] == 1000
    assert row["usage_detail"]["unknown_charged_micro_usd"] == 1000
    assert account["tokens_used"] == 4000 + 1000
    assert account["spent_micro_usd"] == 4000 + 1000
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_a_row_that_cannot_grow_refuses_a_call_that_fits_only_by_reusing_an_unknown_calls_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    # No call_reserve_tokens: the reservation never grows, so the next call must fit in what is left.
    db, gateway = _setup(monkeypatch, reserved=2000, detail={})
    _open(gateway, 1, 1000)
    gateway._note_upstream_failure(_session(), GatewayDenied("upstream_error", sent=True), release_pending=True)

    with pytest.raises(GatewayDenied) as caught:
        _open(gateway, 2, 1001)

    assert caught.value.code == "budget_exceeded"
    assert db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]["pending_tokens"] == 0
    assert db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]["unknown_reserved_tokens"] == 1000


def test_a_call_that_fits_beside_an_unknown_call_is_still_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch, reserved=2000, detail={})
    _open(gateway, 1, 1000)
    gateway._note_upstream_failure(_session(), GatewayDenied("upstream_error", sent=True), release_pending=True)

    _open(gateway, 2, 1000)

    assert db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]["pending_tokens"] == 1000


def test_the_reservation_gap_counts_unknown_tokens_as_occupied() -> None:
    gap = gateway_model.coding_reservation_gap(
        reserved_tokens=4096, measured_tokens=0, unknown_tokens=1000, pending_tokens=0, call_reserve_tokens=4096, call_tokens=1000
    )
    assert gap == 1000
