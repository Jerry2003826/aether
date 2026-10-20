"""A connection that breaks while the request is being written may already have reached the provider.

Once the TCP (and TLS) connection is up, any byte of the request may have left this host, so a reset
or broken pipe while writing is settled like a timeout or a bad SSE event: sent, no upstream status,
and the call's hold is charged at settlement as an unknown-usage call. A failure before the
connection is up (refused, DNS, connect timeout, TLS handshake) sent nothing and still releases it.
"""

from __future__ import annotations

import socket
from typing import Any, cast

import pytest
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_jobs import _settle
from ledger_sql_fake import FakeLedgerDatabase, prices_at

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-1"
_CALL = 3000
_BODY = {"model": "coder", "messages": [{"role": "user", "content": "edit"}]}


class _WriteBreaks:
    """A connected socket: the request line and headers go out, then writing the body fails."""

    def __init__(self, error: OSError) -> None:
        self.error = error
        self.sent: list[bytes] = []

    def setsockopt(self, *_args: Any) -> None:
        return None

    def settimeout(self, *_args: Any) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        if self.sent:
            raise self.error
        self.sent.append(bytes(data))

    def close(self) -> None:
        return None


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


def _grant() -> dict[str, Any]:
    return {
        "id": "1",
        "project_id": _PROJECT,
        "task_id": _TASK,
        "scope": {"task_id": _TASK, "request_key": _PARENT, "attempt_id": "1", "capabilities": ["coding:model"], "aliases": ["coder"]},
    }


def _relay_through(monkeypatch: pytest.MonkeyPatch, connect: Any) -> tuple[FakeLedgerDatabase, GatewayDenied]:
    """One answered call (100/50), then a second call whose connection is opened by connect."""
    db = _setup()
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key", timeout=5)
    gateway._coding_call_tokens = _CALL
    gateway._add_measured_usage(_grant(), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    gateway._scope = lambda _token: _grant()  # type: ignore[method-assign]
    gateway._spend_coding_ticket = lambda _session, *, alias, prompt: 64  # type: ignore[method-assign]
    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(GatewayDenied) as denied:
        gateway.relay_chat("job-token-reset", dict(_BODY))
    _settle(BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None)
    return db, denied.value


@pytest.mark.parametrize("error", [ConnectionResetError(104, "Connection reset by peer"), BrokenPipeError(32, "Broken pipe")], ids=["reset", "broken_pipe"])
def test_a_connection_broken_while_writing_the_request_is_charged_at_its_reservation(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    opened: list[_WriteBreaks] = []

    def connect(*_args: Any, **_kwargs: Any) -> _WriteBreaks:
        sock = _WriteBreaks(error)
        opened.append(sock)
        return sock

    db, denied = _relay_through(monkeypatch, connect)
    assert len(opened) == 1 and opened[0].sent, "the request headers never reached the socket"
    assert (denied.code, denied.sent, denied.upstream_status) == ("upstream_error", True, None)
    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    assert row["usage_detail"]["unknown_usage_calls"] == 1
    assert row["usage_detail"]["unknown_reserved_tokens"] == _CALL
    assert row["usage_detail"]["pending_tokens"] == 0
    assert row["usage_status"] == "known"
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["tokens_used"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_a_refused_connection_sent_nothing_and_releases_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionRefusedError(111, "Connection refused")

    db, denied = _relay_through(monkeypatch, connect)
    assert (denied.code, denied.sent, denied.upstream_status) == ("upstream_error", False, None)
    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    assert "unknown_usage_calls" not in row["usage_detail"]
    assert "unknown_reserved_tokens" not in row["usage_detail"]
    assert row["usage_detail"]["pending_tokens"] == 0
    assert row["usage_status"] == "known"
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)
    assert account["spent_micro_usd"] == 150
    assert account["tokens_used"] == 150
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0
