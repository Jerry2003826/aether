"""Every coding sub-call counts against the task's max_model_requests."""

from __future__ import annotations

from typing import cast

import pytest

import aether_gateway.model as gateway_model
from aether_gateway.model import CODING_MAX_MODEL_CALLS, GatewayDenied, ModelGateway
from aether_storage.db import Database
from ledger_sql_fake import FakeLedgerDatabase

_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-1-7"


def _setup(monkeypatch: pytest.MonkeyPatch, *, max_requests: int) -> tuple[FakeLedgerDatabase, ModelGateway]:
    monkeypatch.setattr(gateway_model, "_coding_lease_held", lambda _conn, **_fence: True)
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, max_requests=max_requests, requests_admitted=1, held_micro_usd=40_000, tokens_held=40_000)
    db.add_request(_PROJECT, _TASK, _PARENT, reserved_micro_usd=40_000, usage_detail={"reserved_tokens": 40_000})
    return db, ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")


def _open(gateway: ModelGateway, index: int) -> None:
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
    )


def _calls(db: FakeLedgerDatabase) -> int:
    return sum(1 for (_p, _t, key) in db.requests if key.startswith(_PARENT + ":call:"))


def test_max_model_requests_of_one_allows_exactly_one_coding_call(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch, max_requests=1)
    _open(gateway, 1)
    with pytest.raises(GatewayDenied) as caught:
        _open(gateway, 2)
    assert caught.value.code == "budget_exceeded"
    assert _calls(db) == 1
    assert db.account(_PROJECT, _TASK)["requests_admitted"] == 1


def test_each_coding_call_after_the_first_is_admitted_against_max_model_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch, max_requests=3)
    for index in range(1, 4):
        _open(gateway, index)
    assert db.account(_PROJECT, _TASK)["requests_admitted"] == 3
    with pytest.raises(GatewayDenied) as caught:
        _open(gateway, 4)
    assert caught.value.code == "budget_exceeded"
    assert _calls(db) == 3
    assert db.account(_PROJECT, _TASK)["requests_admitted"] == 3


def test_the_per_job_call_cap_still_applies_under_a_large_request_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    db, gateway = _setup(monkeypatch, max_requests=48)
    for index in range(1, CODING_MAX_MODEL_CALLS + 1):
        _open(gateway, index)
    with pytest.raises(GatewayDenied) as caught:
        _open(gateway, CODING_MAX_MODEL_CALLS + 1)
    assert caught.value.code == "budget_exceeded"
    assert _calls(db) == CODING_MAX_MODEL_CALLS
    assert db.account(_PROJECT, _TASK)["requests_admitted"] == CODING_MAX_MODEL_CALLS
