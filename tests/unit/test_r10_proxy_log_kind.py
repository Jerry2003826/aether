"""Every coding proxy log line names the request kind, its safe path and a request number (live run on d0b4e7f).

Each round of a live run logged five `coding_proxy status=200 error=` lines but the job had four
coding_call rows. The fifth line was the sandbox's in-band POST /aether/revoke
(openhands_sandbox_entry.close_model_channel), which the proxy answers 200 without forwarding anything
and which logged exactly like a chat completion. The line is now

    coding_proxy kind=<chat|revoke|other|none> path=<chat or revoke path, else -> request=<n> status=<code> error=<code>

so counting `kind=chat ... status=200` lines gives the calls the gateway accepted, a revoke is labelled
revoke, and a refused call (409 budget_exceeded, 401 revoked or unauthorized) is never a chat success.
Only the two fixed endpoint paths are logged; any other path, the job token, the loopback token and the
provider key never are.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest
from aether_storage.db import Database
from aether_worker.coding_model_proxy import REVOKE_PATH, CodingModelProxy

_JOB = "job-token-proxy-log-kind"
_LOOP = "loopback-proxy-log-kind"
_SECRET_PATH = "/v1/a-fixed-secret-looking-path-phrase"
_REFUSED_PROMPT = "这次调用超出预算"
_LOGGER = "aether.worker.coding_proxy"
_LINE = re.compile(r"^coding_proxy kind=(chat|revoke|other|none) path=(\S+) request=(\d+) status=(\d+) error=(\S*)$")


class _Gateway(BaseHTTPRequestHandler):
    """The loopback gateway: a call it accepts is one coding_call row; the refused prompt is denied."""

    accepted: list[str]

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or "0")))
        prompt = body["messages"][-1]["content"]
        if prompt == _REFUSED_PROMPT:
            answer = {"denied": True, "code": "budget_exceeded", "sent": False}
        else:
            type(self).accepted.append(prompt)
            answer = {"id": "ok", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        encoded = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def gateway():
    handler = type("Gateway", (_Gateway,), {"accepted": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, handler
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxy(gateway, monkeypatch):
    server, _handler = gateway
    monkeypatch.setenv("AETHER_GATEWAY_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", _LOOP)
    monkeypatch.delenv("AETHER_LIVE_MODEL_KEY", raising=False)
    made = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    try:
        yield made
    finally:
        made.close()


def _request(path: str, body: dict | None = None, *, token: str = _JOB) -> bytes:
    raw = json.dumps(body).encode() if body is not None else b""
    head = f"POST {path} HTTP/1.1\r\nhost: aether-proxy\r\nauthorization: Bearer {token}\r\ncontent-length: {len(raw)}\r\n\r\n"
    return head.encode() + raw


def _connect(proxy: CodingModelProxy) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(str(proxy.socket_path))
    return sock


def _status(sock: socket.socket) -> int:
    reply = b""
    while chunk := sock.recv(65536):
        reply += chunk
    return int(reply.split(b" ", 2)[1])


def _send(proxy: CodingModelProxy, data: bytes) -> int:
    sock = _connect(proxy)
    try:
        sock.sendall(data)
        return _status(sock)
    finally:
        sock.close()


def _chat_body(prompt: str) -> dict:
    return {"model": "coder", "messages": [{"role": "user", "content": prompt}]}


def _lines(caplog) -> list[tuple[str, ...]]:
    found = []
    for record in caplog.records:
        if record.name != _LOGGER:
            continue
        matched = _LINE.match(record.getMessage())
        assert matched is not None, record.getMessage()
        found.append(matched.groups())
    return found


def _chat_200s(lines: list[tuple[str, ...]]) -> int:
    return sum(1 for kind, _path, _number, status, _error in lines if kind == "chat" and status == "200")


def test_a_chat_call_is_labelled_chat_with_its_endpoint(proxy, caplog) -> None:
    caplog.set_level("INFO", logger=_LOGGER)
    assert _send(proxy, _request("/v1/chat/completions", _chat_body("第一次调用"))) == 200
    assert _send(proxy, _request("/chat/completions", _chat_body("第二次调用"))) == 200
    lines = _lines(caplog)
    assert [(kind, path, status, error) for kind, path, _n, status, error in lines] == [
        ("chat", "/v1/chat/completions", "200", ""),
        ("chat", "/chat/completions", "200", ""),
    ]
    assert lines[0][2] != lines[1][2], "each request has its own number"


def test_chat_200s_equal_the_calls_the_gateway_accepted_and_the_revoke_is_labelled(proxy, gateway, caplog) -> None:
    _server, handler = gateway
    caplog.set_level("INFO", logger=_LOGGER)
    for number in range(4):
        assert _send(proxy, _request("/v1/chat/completions", _chat_body(f"第 {number} 次调用"))) == 200
    assert _send(proxy, _request(REVOKE_PATH)) == 200
    lines = _lines(caplog)
    assert len(lines) == 5
    assert lines[-1][:2] == ("revoke", REVOKE_PATH) and lines[-1][3:] == ("200", "")
    assert _chat_200s(lines) == len(handler.accepted) == 4
    assert len({number for _k, _p, number, _s, _e in lines}) == 5


def test_a_budget_refusal_is_a_chat_409_not_a_chat_success(proxy, gateway, caplog) -> None:
    _server, handler = gateway
    caplog.set_level("INFO", logger=_LOGGER)
    assert _send(proxy, _request("/v1/chat/completions", _chat_body("被接受的调用"))) == 200
    assert _send(proxy, _request("/v1/chat/completions", _chat_body(_REFUSED_PROMPT))) == 409
    lines = _lines(caplog)
    assert [(kind, status, error) for kind, _p, _n, status, error in lines] == [("chat", "200", ""), ("chat", "409", "budget_exceeded")]
    assert _chat_200s(lines) == len(handler.accepted) == 1


def test_a_chat_refused_by_the_revoke_is_a_chat_401_not_a_chat_success(proxy, gateway, caplog) -> None:
    _server, handler = gateway
    caplog.set_level("INFO", logger=_LOGGER)
    raw = json.dumps(_chat_body("撤销后到达的调用")).encode()
    late = _connect(proxy)
    try:
        # The head passes the screen before the revoke; the body arrives after it.
        late.sendall(
            f"POST /v1/chat/completions HTTP/1.1\r\nauthorization: Bearer {_JOB}\r\ncontent-length: {len(raw)}\r\n\r\n".encode()
        )
        time.sleep(0.2)
        assert _send(proxy, _request(REVOKE_PATH)) == 200
        late.sendall(raw)
        assert _status(late) == 401
    finally:
        late.close()
    lines = _lines(caplog)
    assert sorted((kind, status, error) for kind, _p, _n, status, error in lines) == [("chat", "401", "revoked"), ("revoke", "200", "")]
    assert _chat_200s(lines) == len(handler.accepted) == 0


def test_a_wrong_token_is_a_chat_401_unauthorized(proxy, caplog) -> None:
    caplog.set_level("INFO", logger=_LOGGER)
    assert _send(proxy, _request("/v1/chat/completions", _chat_body("错误令牌"), token="wrong-token-fixed-phrase")) == 401
    assert [(kind, status, error) for kind, _p, _n, status, error in _lines(caplog)] == [("chat", "401", "unauthorized")]
    assert "wrong-token-fixed-phrase" not in caplog.text


def test_another_path_is_other_and_no_path_token_or_key_is_logged(proxy, caplog) -> None:
    caplog.set_level("INFO", logger=_LOGGER)
    assert _send(proxy, _request(_SECRET_PATH + "?token=fixed-query-phrase", {"model": "coder"})) == 404
    assert [(kind, path, status, error) for kind, path, _n, status, error in _lines(caplog)] == [("other", "-", "404", "not_found")]
    for secret in (_SECRET_PATH, "fixed-query-phrase", _JOB, _LOOP):
        assert secret not in caplog.text
