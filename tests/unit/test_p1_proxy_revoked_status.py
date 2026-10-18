"""The proxy's answer after a revoke depends on the channel, not on the body; drain counts calls.

Once the channel is revoked, every chat POST with the job token gets 401 revoked before its body is
read or parsed: nothing is forwarded or charged. Before the revoke a bad body is 400 body_rejected;
a wrong token is always 401 unauthorized. The drain's "unfinished" is forwarding_calls, the calls
handed to the gateway, not open connections: an idle connection, or one still sending its body, is
not a call, and proxy_drained / drain_unfinished / unknown_usage_calls are written only for real ones.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker import coding_jobs
from aether_worker.coding_model_proxy import CodingModelProxy
from ledger_sql_fake import FakeLedgerDatabase

_JOB = "job-token-revoked-status"
_KEY = "coding-job-drain-1"


class _Gateway(BaseHTTPRequestHandler):
    entered = threading.Event()
    release = threading.Event()
    block = False

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or "0"))
        if type(self).block:
            type(self).entered.set()
            type(self).release.wait(10)
        encoded = json.dumps({"id": "ok", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[CodingModelProxy]:
    _Gateway.entered = threading.Event()
    _Gateway.release = threading.Event()
    _Gateway.block = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AETHER_GATEWAY_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-revoked-status")
    monkeypatch.setenv("AETHER_CODING_PROXY_READ_TIMEOUT_SECONDS", "8")
    monkeypatch.delenv("AETHER_LIVE_MODEL_KEY", raising=False)
    made = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    try:
        yield made
    finally:
        _Gateway.release.set()
        made.close()
        server.shutdown()
        server.server_close()


def _connect(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(4)
    sock.connect(str(path))
    return sock


def _head(token: str, length: int) -> bytes:
    return (
        f"POST /v1/chat/completions HTTP/1.1\r\nauthorization: Bearer {token}\r\ncontent-length: {length}\r\nconnection: close\r\n\r\n"
    ).encode("ascii")


def _reply(sock: socket.socket) -> tuple[int, dict]:
    chunks = []
    while block := sock.recv(65536):
        chunks.append(block)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), json.loads(body or b"{}")


def _wait_connected(proxy: CodingModelProxy, count: int) -> None:
    assert proxy.wait_until_active(count, timeout=5), "the connection never reached the proxy"


def test_after_revoke_a_bad_body_is_401_revoked_not_400(proxy: CodingModelProxy) -> None:
    sock = _connect(proxy.socket_path)
    try:
        _wait_connected(proxy, 1)
        proxy.revoke()
        body = b"{not json"
        sock.sendall(_head(_JOB, len(body)) + body)
        assert _reply(sock) == (401, {"error": "revoked"})
    finally:
        sock.close()
    assert proxy.respond("POST", "/v1/chat/completions", {"authorization": f"Bearer {_JOB}"}, b"[1, 2") == (401, {"error": "revoked"})
    assert proxy.respond("POST", "/v1/chat/completions", {"authorization": f"Bearer {_JOB}"}, b"[]") == (401, {"error": "revoked"})


def test_after_revoke_the_answer_comes_before_the_body_is_read(proxy: CodingModelProxy) -> None:
    sock = _connect(proxy.socket_path)
    try:
        _wait_connected(proxy, 1)
        proxy.revoke()
        started = time.monotonic()
        # Headers announce a body that is never sent: the revoke answer must not wait for it.
        sock.sendall(_head(_JOB, 4096))
        assert _reply(sock) == (401, {"error": "revoked"})
        assert time.monotonic() - started < 3
    finally:
        sock.close()


def test_a_bad_token_is_unauthorized_before_and_after_revoke(proxy: CodingModelProxy) -> None:
    assert proxy.respond("POST", "/v1/chat/completions", {"authorization": "Bearer wrong"}, b"{}") == (401, {"error": "unauthorized"})
    proxy.revoke()
    assert proxy.respond("POST", "/v1/chat/completions", {"authorization": "Bearer wrong"}, b"{}") == (401, {"error": "unauthorized"})


def test_before_revoke_a_bad_body_is_still_400(proxy: CodingModelProxy) -> None:
    assert proxy.respond("POST", "/v1/chat/completions", {"authorization": f"Bearer {_JOB}"}, b"{not json") == (400, {"error": "body_rejected"})
    sock = _connect(proxy.socket_path)
    try:
        body = b"[1]"
        sock.sendall(_head(_JOB, len(body)) + body)
        assert _reply(sock) == (400, {"error": "body_rejected"})
    finally:
        sock.close()


def _ledger_row() -> FakeLedgerDatabase:
    db = FakeLedgerDatabase()
    db.add_account("p", "t", held_micro_usd=8192, tokens_held=8192, requests_admitted=1)
    db.add_request("p", "t", _KEY, reserved_micro_usd=8192, usage_detail={"reserved_tokens": 8192, "pending_tokens": 3000})
    return db


def test_drain_counts_forwarding_calls_not_open_connections(proxy: CodingModelProxy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "1")
    idle = _connect(proxy.socket_path)
    sending = _connect(proxy.socket_path)
    try:
        _wait_connected(proxy, 2)
        # One connection never sends anything; the other is still sending its body.
        sending.sendall(_head(_JOB, 4096) + b'{"model":')
        db = _ledger_row()
        started = time.monotonic()
        coding_jobs._drain_proxy(proxy, BudgetLedger(cast(Database, db)), owner_id="o", project_id="p", task_id="t", request_key=_KEY)
        assert time.monotonic() - started < 3, "the drain waited on connections that carry no call"
        assert proxy.forwarding_calls == 0
        assert proxy.drain(timeout=0.2) == 0
        detail = db.request("p", "t", _KEY)["usage_detail"]
        assert "proxy_drained" not in detail
        assert "drain_unfinished" not in detail
        assert "unknown_usage_calls" not in detail
        assert detail["pending_tokens"] == 3000
    finally:
        idle.close()
        sending.close()


def test_drain_counts_a_call_still_forwarding(proxy: CodingModelProxy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "0.3")
    _Gateway.block = True
    caller = _connect(proxy.socket_path)
    idle = _connect(proxy.socket_path)
    try:
        body = json.dumps({"model": "coder", "messages": []}).encode()
        caller.sendall(_head(_JOB, len(body)) + body)
        assert _Gateway.entered.wait(5)
        assert proxy.forwarding_calls == 1
        db = _ledger_row()
        coding_jobs._drain_proxy(proxy, BudgetLedger(cast(Database, db)), owner_id="o", project_id="p", task_id="t", request_key=_KEY)
        detail = db.request("p", "t", _KEY)["usage_detail"]
        # One call was forwarding; the idle connection is not counted.
        assert detail["proxy_drained"] is True
        assert detail["drain_unfinished"] == 1
        assert detail["unknown_usage_calls"] == 1
        assert detail["unknown_reserved_tokens"] == 3000
    finally:
        _Gateway.release.set()
        caller.close()
        idle.close()


def test_drain_closes_the_channel_so_a_waiting_connection_cannot_start_a_call(proxy: CodingModelProxy) -> None:
    sock = _connect(proxy.socket_path)
    try:
        _wait_connected(proxy, 1)
        assert proxy.drain(timeout=0.2) == 0
        body = json.dumps({"model": "coder", "messages": []}).encode()
        sock.sendall(_head(_JOB, len(body)) + body)
        assert _reply(sock) == (401, {"error": "revoked"})
        assert proxy.forwarding_calls == 0
    finally:
        sock.close()
