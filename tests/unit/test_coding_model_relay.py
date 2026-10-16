"""Coding model calls use ModelGateway.relay_chat and the job-token socket."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from aether_gateway.__main__ import GATEWAY_CHAT_TIMEOUT_SECONDS, gateway_timeout
from aether_gateway.model import (
    GatewayDenied,
    ModelGateway,
    _fencing_token,
    _uuid_text,
    accepted_chat_body,
    scripted_chat_completion,
)
from aether_storage.db import Database
from aether_worker.coding_model_proxy import CodingModelProxy

_KEY = "gateway-provider-key"
_JOB = "job-token-unit"
_LOOP = "loop-token-unit"


def _grant(*, capabilities: list[str] | None = None) -> dict:
    return {
        "id": "1",
        "project_id": "p",
        "task_id": "t",
        "scope": {
            "task_id": "t",
            "request_key": "coding-1",
            "attempt_id": "1",
            "capabilities": ["coding:model"] if capabilities is None else capabilities,
            "aliases": ["coder"],
        },
    }


def _gateway(upstream: str) -> ModelGateway:
    gateway = ModelGateway(cast(Database, object()), upstream_base=upstream, provider_key=_KEY, timeout=5)
    gateway._scope = lambda token: _grant() if token == _JOB else (_ for _ in ()).throw(GatewayDenied("token_rejected"))  # type: ignore[method-assign]
    gateway._spend_coding_ticket = lambda _session, *, alias, prompt: 64  # type: ignore[method-assign]
    gateway._add_measured_usage = lambda _session, _result: None  # type: ignore[method-assign]
    return gateway


def test_coding_fence_identity_rejects_a_non_decimal_attempt() -> None:
    assert _fencing_token("7") == 7
    try:
        _fencing_token("not-a-fence")
    except GatewayDenied as exc:
        assert exc.code == "token_rejected"
        assert exc.sent is False
    else:
        raise AssertionError("非数字 fencing 应拒绝")
    try:
        _uuid_text("not-a-job")
    except GatewayDenied as exc:
        assert exc.code == "token_rejected"
    else:
        raise AssertionError("非法作业 id 应拒绝")
    try:
        _fencing_token(str(9223372036854775808))
    except GatewayDenied as exc:
        assert exc.code == "token_rejected"
    else:
        raise AssertionError("超出 bigint 的 fencing 应拒绝")


def test_chat_body_keeps_only_the_allowlist_and_the_server_timeout() -> None:
    cleaned = accepted_chat_body(
        {
            "model": "coder",
            "messages": [{"role": "user", "content": "edit"}],
            "tools": [{"type": "function"}],
            "tool_choice": "auto",
            "timeout": 1e12,
            "stream": True,
            "user": "root",
            "max_tokens": 99999,
        },
        max_tokens=64,
    )
    assert cleaned == {
        "model": "coder",
        "messages": [{"role": "user", "content": "edit"}],
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
        "max_tokens": 64,
    }
    source = Path("services/gateway/aether_gateway/__main__.py").read_text(encoding="utf-8")
    assert "timeout=gateway_timeout(path, body)" in source
    assert gateway_timeout("/v1/chat/completions", {"timeout": 1e12}) == GATEWAY_CHAT_TIMEOUT_SECONDS
    assert gateway_timeout("/v1/chat/completions", {}) == 30.0
    assert gateway_timeout("/v1/complete", {"timeout": 12.5}) == 12.5
    assert gateway_timeout("/v1/complete", {"timeout": 0}) == 30.0


def test_empty_upstream_returns_a_finish_call_with_token_usage() -> None:
    gateway = _gateway("")
    body = gateway.relay_chat(_JOB, {"model": "coder", "messages": [{"role": "user", "content": "edit"}]})
    assert body == scripted_chat_completion("coder")
    usage = body["usage"]
    assert usage["prompt_tokens"] == 8
    assert usage["completion_tokens"] == 4
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "finish"


def test_relay_posts_the_provider_key_and_ignores_http_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            raw = self.rfile.read(length)
            seen["authorization"] = self.headers.get("authorization")
            seen["path"] = self.path
            seen["body"] = json.loads(raw)
            encoded = json.dumps(
                {
                    "id": "chatcmpl-upstream",
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 11, "total_tokens": 31},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        gateway = _gateway(f"http://127.0.0.1:{port}/v1")
        body = gateway.relay_chat(
            _JOB,
            {
                "model": "openai/coder",
                "messages": [{"role": "user", "content": "edit"}],
                "tools": [{"type": "function", "function": {"name": "file_editor"}}],
                "tool_choice": "auto",
                "aether_job_token": _JOB,
                "timeout": 1e12,
                "stream": True,
                "user": "root",
                "max_tokens": 99999,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
    assert seen["authorization"] == f"Bearer {_KEY}"
    assert seen["path"] == "/v1/chat/completions"
    forwarded = seen["body"]
    assert isinstance(forwarded, dict)
    assert "aether_job_token" not in forwarded
    assert "timeout" not in forwarded
    assert "stream" not in forwarded
    assert "user" not in forwarded
    assert forwarded["max_tokens"] == 64
    assert forwarded["tool_choice"] == "auto"
    assert forwarded["model"] == "openai/coder"
    assert body["usage"]["prompt_tokens"] == 20
    assert body["usage"]["completion_tokens"] == 11


def test_relay_rejects_the_wrong_capability_alias_and_a_leaked_key() -> None:
    denied = _gateway("")
    denied._scope = lambda _token: _grant(capabilities=["research:model"])  # type: ignore[method-assign]
    try:
        denied.relay_chat(_JOB, {"model": "coder"})
    except GatewayDenied as exc:
        assert exc.code == "capability_denied"
    else:
        raise AssertionError("capability was accepted")
    wrong_alias = _gateway("")
    try:
        wrong_alias.relay_chat(_JOB, {"model": "planner"})
    except GatewayDenied as exc:
        assert exc.code == "alias_denied"
    else:
        raise AssertionError("alias was accepted")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            self.rfile.read(length)
            encoded = json.dumps({"echo": _KEY}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = _gateway(f"http://127.0.0.1:{server.server_address[1]}/v1")
        try:
            gateway.relay_chat(_JOB, {"model": "coder"})
        except GatewayDenied as exc:
            assert exc.code == "secret_leak"
            assert exc.sent is True
        else:
            raise AssertionError("provider key was returned")
    finally:
        server.shutdown()
        server.server_close()


def test_socket_proxy_checks_the_job_token_and_forwards_to_the_gateway(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            raw = self.rfile.read(length)
            seen["authorization"] = self.headers.get("authorization")
            seen["body"] = json.loads(raw)
            encoded = json.dumps({"id": "from-gateway", "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AETHER_GATEWAY_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", _LOOP)
    monkeypatch.delenv("AETHER_LIVE_MODEL_KEY", raising=False)
    proxy = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    try:
        refused = _ask(proxy.socket_path, "Bearer other-token", {"model": "coder"})
        accepted = _ask(proxy.socket_path, f"Bearer {_JOB}", {"model": "coder", "messages": []})
        assert proxy.directory.stat().st_mode & 0o007 == 0
        assert proxy.socket_path.stat().st_mode & 0o007 == 0
        assert _acl_perm(proxy.directory, 0x02, 65534) & 0o1
        assert _acl_perm(proxy.socket_path, 0x02, 65534) & 0o6 == 0o6
        assert _acl_perm(proxy.directory, 0x20, 0xFFFFFFFF) == 0
        assert _acl_perm(proxy.socket_path, 0x20, 0xFFFFFFFF) == 0
    finally:
        proxy.close()
        server.shutdown()
        server.server_close()
    assert refused[0] == 401
    assert accepted[0] == 200
    assert accepted[1]["id"] == "from-gateway"
    assert seen["authorization"] == f"Bearer {_LOOP}"
    forwarded = seen["body"]
    assert isinstance(forwarded, dict)
    assert forwarded["aether_job_token"] == _JOB
    assert os.environ.get("AETHER_LIVE_MODEL_KEY") in {None, ""}


def _acl_perm(path: Path, tag: int, ident: int) -> int:
    raw = os.getxattr(path, "system.posix_acl_access", follow_symlinks=False)
    version = struct.unpack_from("<I", raw)[0]
    assert version == 2
    offset = 4
    while offset + 8 <= len(raw):
        found_tag, perm, found_ident = struct.unpack_from("<HHI", raw, offset)
        if found_tag == tag and found_ident == ident:
            return perm
        offset += 8
    raise AssertionError((tag, ident))


def _ask(path, authorization: str, payload: dict) -> tuple[int, dict]:
    raw = json.dumps(payload).encode()
    request = (
        "POST /v1/chat/completions HTTP/1.1\r\n"
        f"authorization: {authorization}\r\n"
        "content-type: application/json\r\n"
        f"content-length: {len(raw)}\r\n"
        "connection: close\r\n"
        "\r\n"
    ).encode("ascii") + raw
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
        sock.sendall(request)
        chunks: list[bytes] = []
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
    finally:
        sock.close()
    response = b"".join(chunks)
    head, _, body = response.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, json.loads(body)
