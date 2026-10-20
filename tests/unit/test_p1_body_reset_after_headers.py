"""Once a status line and headers came back, the call was sent, whatever happens to the body.

A body cut while it is read (a connection reset, a body shorter than its Content-Length, the byte cap
or the overall deadline) is a "response cut": the call is sent and, without usage, charged at its
reservation. That holds for a non-2xx reply too: a 4xx/5xx whose body is cut is charged at the
reservation and keeps both upstream_status and error_body_truncated on the row and in the relay log
line. A non-2xx reply whose body arrived in full is a known HTTP error and is not charged. A failure
while reading an error body is a gateway denial, never a raw exception out of the relay.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
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
_PARENT = "coding-job-cut"
_CALL = 3000
_BODY = {"model": "coder", "messages": [{"role": "user", "content": "edit"}]}


class _RawUpstream:
    """A TCP upstream that answers with the given head and body bytes, then ends the connection.

    end is "reset" (RST through SO_LINGER 0), "close" (a normal FIN) or "hang" (keep the socket open).
    """

    def __init__(self, head: bytes, body: bytes, *, end: str) -> None:
        self.head = head
        self.body = body
        self.end = end
        self.release = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.listener.getsockname()[1]}/v1"

    def _serve(self) -> None:
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        try:
            conn.settimeout(5)
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                received += chunk
            head, _, rest = received.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(rest) < length:
                rest += conn.recv(65536)
            conn.sendall(self.head + self.body)
            time.sleep(0.05)
            if self.end == "hang":
                self.release.wait(10)
            if self.end == "reset":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            conn.close()

    def close(self) -> None:
        self.release.set()
        self.listener.close()


def _head(status: str, *, length: int, content_type: str = "application/json") -> bytes:
    return (f"HTTP/1.1 {status}\r\ncontent-type: {content_type}\r\ncontent-length: {length}\r\nconnection: close\r\n\r\n").encode()


def _setup() -> FakeLedgerDatabase:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=8192, tokens_held=8192, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=8192,
        usage_detail={"reserved_tokens": 8192, "call_reserve_tokens": 4096, "call_reserve_micro_usd": 4096, **prices_at(4096, 4096), "pending_tokens": 2 * _CALL},
    )
    return db


def _grant() -> dict[str, Any]:
    return {
        "id": "1",
        "project_id": _PROJECT,
        "task_id": _TASK,
        "scope": {"task_id": _TASK, "request_key": _PARENT, "attempt_id": "1", "capabilities": ["coding:model"], "aliases": ["coder"]},
    }


def _relay(upstream: _RawUpstream) -> tuple[FakeLedgerDatabase, BaseException]:
    """One answered call (100/50), then a call to the raw upstream; the attempt is then settled."""
    db = _setup()
    gateway = ModelGateway(cast(Database, db), upstream_base=upstream.base, provider_key="unit-key", timeout=2)
    gateway._coding_call_tokens = _CALL
    gateway._add_measured_usage(_grant(), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    gateway._scope = lambda _token: _grant()  # type: ignore[method-assign]
    gateway._spend_coding_ticket = lambda _session, *, alias, prompt: 64  # type: ignore[method-assign]
    try:
        with pytest.raises(BaseException) as raised:
            gateway.relay_chat("job-token-cut", dict(_BODY))
    finally:
        upstream.close()
    _settle(BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None)
    return db, raised.value


def _assert_charged_at_the_reservation(db: FakeLedgerDatabase) -> None:
    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    assert row["usage_detail"]["unknown_usage_calls"] == 1
    assert row["usage_detail"]["unknown_reserved_tokens"] == _CALL
    assert row["usage_detail"]["pending_tokens"] == 0
    assert row["usage_status"] == "known"
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["tokens_used"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def _assert_not_charged(db: FakeLedgerDatabase) -> None:
    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    assert "unknown_usage_calls" not in row["usage_detail"]
    assert "unknown_reserved_tokens" not in row["usage_detail"]
    assert account["spent_micro_usd"] == 150
    assert account["tokens_used"] == 150
    assert account["held_micro_usd"] == 0


_PARTIAL_JSON = b'{"id":"c1","choices":[{"index":0,"message":{"role":"assistant","content":"par'


def test_a_reset_reading_a_2xx_body_is_sent_and_charged_at_the_reservation() -> None:
    upstream = _RawUpstream(_head("200 OK", length=4000), _PARTIAL_JSON, end="reset")
    db, raised = _relay(upstream)
    assert isinstance(raised, GatewayDenied), repr(raised)
    assert (raised.code, raised.sent, raised.upstream_status) == ("upstream_error", True, None)
    _assert_charged_at_the_reservation(db)


def test_an_incomplete_read_of_a_2xx_body_is_sent() -> None:
    upstream = _RawUpstream(_head("200 OK", length=4000), _PARTIAL_JSON, end="close")
    db, raised = _relay(upstream)
    assert isinstance(raised, GatewayDenied), repr(raised)
    assert (raised.code, raised.sent, raised.upstream_status) == ("upstream_error", True, None)
    _assert_charged_at_the_reservation(db)


def test_a_2xx_body_shorter_than_its_content_length_is_cut_even_when_it_parses() -> None:
    """The connection closed before the declared length: what arrived is not the whole reply."""
    whole = (
        b'{"id":"c1","model":"coder","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}'
    )
    upstream = _RawUpstream(_head("200 OK", length=len(whole) + 500), whole, end="close")
    db, raised = _relay(upstream)
    assert isinstance(raised, GatewayDenied), repr(raised)
    assert (raised.code, raised.sent, raised.upstream_status) == ("upstream_error", True, None)
    _assert_charged_at_the_reservation(db)


def test_a_response_that_started_is_sent_even_if_the_connect_hook_never_fired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sent follows from the status line that came back, not from the hook that watches connect()."""

    def connect_without_hook(self: Any) -> None:
        gateway_model.http.client.HTTPConnection.connect(self)

    monkeypatch.setattr(gateway_model._SeenHTTPConnection, "connect", connect_without_hook)
    upstream = _RawUpstream(_head("200 OK", length=4000), _PARTIAL_JSON, end="reset")
    db, raised = _relay(upstream)
    assert isinstance(raised, GatewayDenied), repr(raised)
    assert (raised.code, raised.sent) == ("upstream_error", True)
    _assert_charged_at_the_reservation(db)


@pytest.mark.parametrize("end", ["reset", "close"], ids=["connection_reset", "incomplete_read"])
def test_a_reset_reading_a_non_2xx_body_is_a_gateway_denial_not_a_raw_exception(end: str) -> None:
    upstream = _RawUpstream(_head("500 Internal Server Error", length=4000), b'{"error":{"code":"server_er', end=end)
    _db, raised = _relay(upstream)
    assert isinstance(raised, GatewayDenied), repr(raised)
    assert raised.sent is True
    assert raised.upstream_status == 500


@pytest.mark.parametrize("cut", ["reset", "incomplete_read", "byte_cap", "deadline"])
def test_a_cut_non_2xx_body_is_charged_at_the_reservation_and_logs_status_and_truncation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cut: str
) -> None:
    caplog.set_level(logging.INFO, logger="aether.gateway.model")
    body = b'{"error":{"code":"server_error","message":"' + b"x" * 200
    if cut == "reset":
        upstream = _RawUpstream(_head("500 Internal Server Error", length=4000), body, end="reset")
    elif cut == "incomplete_read":
        upstream = _RawUpstream(_head("500 Internal Server Error", length=4000), body, end="close")
    elif cut == "byte_cap":
        monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "64")
        upstream = _RawUpstream(_head("500 Internal Server Error", length=len(body)), body, end="close")
    else:
        monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "0.5")
        upstream = _RawUpstream(_head("500 Internal Server Error", length=4000), body, end="hang")
    db, raised = _relay(upstream)
    assert isinstance(raised, GatewayDenied), repr(raised)
    assert raised.sent is True
    assert raised.upstream_status == 500
    _assert_charged_at_the_reservation(db)
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    assert detail["upstream_status"] == 500
    assert detail["error_body_truncated"] is True
    relay_lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("relay project=")]
    assert relay_lines, caplog.text
    assert "status=500 " in relay_lines[-1]
    assert "error_body_truncated=true" in relay_lines[-1]


def test_a_complete_non_2xx_body_is_still_not_charged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="aether.gateway.model")
    body = b'{"error":{"code":"rate_limited","message":"slow down"}}'
    upstream = _RawUpstream(_head("429 Too Many Requests", length=len(body)), body, end="close")
    db, raised = _relay(upstream)
    assert isinstance(raised, GatewayDenied), repr(raised)
    assert (raised.code, raised.sent, raised.upstream_status) == ("upstream_http_429", True, 429)
    _assert_not_charged(db)
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    assert detail["upstream_status"] == 429
    assert "error_body_truncated" not in detail
