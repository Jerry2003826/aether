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
    CODING_CALL_OUTPUT_CAP,
    GatewayDenied,
    ModelGateway,
    _fencing_token,
    _uuid_text,
    accepted_chat_body,
    coding_reservation_gap,
    coding_reservation_micro,
)
from aether_worker.role_launch import _gateway_env
from aether_storage.db import Database
from aether_worker.coding_model_proxy import CodingModelProxy

def test_per_call_reserve_grows_by_the_shortfall_and_legacy_rows_do_not() -> None:
    assert coding_reservation_gap(reserved_tokens=256, measured_tokens=5, pending_tokens=0, call_reserve_tokens=None) is None
    assert coding_reservation_gap(reserved_tokens=4096, measured_tokens=0, pending_tokens=0, call_reserve_tokens=4096) == 0
    assert coding_reservation_gap(reserved_tokens=4096, measured_tokens=2800, pending_tokens=0, call_reserve_tokens=4096) == 2800
    assert (
        coding_reservation_gap(
            reserved_tokens=4096,
            measured_tokens=2800,
            pending_tokens=CODING_CALL_OUTPUT_CAP,
            call_reserve_tokens=4096,
        )
        == 2800 + CODING_CALL_OUTPUT_CAP
    )
    assert coding_reservation_micro(2800, call_tokens=4096, call_micro=4096) == 2800
    assert coding_reservation_micro(1, call_tokens=4096, call_micro=8192) == 2


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
    gateway._note_upstream_failure = lambda _session, _exc, *, release_pending: None  # type: ignore[method-assign]
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


def test_empty_upstream_is_model_unavailable_and_returns_no_completion() -> None:
    """Without an upstream there is no model: no tool call, no scripted completion, no spent ticket."""
    gateway = _gateway("")
    notes: list[str] = []
    spent: list[str] = []
    gateway._note_admission_block = lambda _session, code: notes.append(code)  # type: ignore[method-assign]

    def spend(_session: object, *, alias: str, prompt: str) -> int:
        spent.append(prompt)
        return 64

    gateway._spend_coding_ticket = spend  # type: ignore[method-assign]
    for body in (
        {"model": "coder", "messages": [{"role": "user", "content": "edit"}]},
        {"model": "openai/coder", "messages": [{"role": "tool", "tool_call_id": "x", "content": "ok"}]},
    ):
        try:
            returned = gateway.relay_chat(_JOB, body)
        except GatewayDenied as exc:
            assert exc.code == "model_unavailable"
            assert exc.sent is False
        else:
            raise AssertionError(f"no-upstream relay returned a completion: {returned!r}")
    assert notes == ["model_unavailable", "model_unavailable"]
    assert spent == []


def test_production_code_has_no_scripted_coding_model() -> None:
    import aether_gateway.model as gateway_model

    assert not hasattr(gateway_model, "scripted_chat_completion")
    assert not hasattr(gateway_model, "SCRIPTED_EDIT_COMMAND")
    banned = ("scripted_chat_completion", "SCRIPTED_EDIT_COMMAND", "chatcmpl-aether-scripted", "sed -i")
    found = [
        f"{path}: {word}"
        for path in sorted(Path("services").rglob("*.py"))
        for word in banned
        if word in path.read_text(encoding="utf-8")
    ]
    assert found == []


def test_a_model_unavailable_job_fails_even_when_the_files_pass() -> None:
    from aether_worker.coding_jobs import _coding_terminal

    result = {
        "passed": 1,
        "failed": 0,
        "model_requests": 0,
        "agent_completed": False,
        "patch": "--- a/app.py\n+++ b/app.py\n",
        "credential_present": False,
        "network_errno": 101,
    }
    assert _coding_terminal(
        {"exit_code": 0, "result": result},
        upstream_error=None,
        measured=None,
        usage_missing=False,
        admission_error="model_unavailable",
    ) == ("FAILED", "model_unavailable")


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
    assert "user" not in forwarded
    assert forwarded["stream"] is True
    assert forwarded["stream_options"] == {"include_usage": True}
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


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _sse(events: list[dict]) -> bytes:
    blocks = [f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events]
    blocks.append("data: [DONE]\n\n")
    return "".join(blocks).encode()


_DEEPSEEK_USAGE = {
    "prompt_tokens": 18,
    "completion_tokens": 9,
    "total_tokens": 27,
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 18,
    "prompt_tokens_details": {"cached_tokens": 0},
}


def test_live_alias_replaces_the_upstream_model_and_stream_forces_include_usage(monkeypatch) -> None:
    monkeypatch.setenv("AETHER_LIVE_MODEL_ALIAS", "deepseek-chat")
    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            seen["body"] = json.loads(self.rfile.read(length))
            encoded = json.dumps(
                {
                    "id": "chatcmpl-deepseek",
                    "object": "chat.completion",
                    "model": "deepseek-chat",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": "stop"}],
                    "usage": dict(_DEEPSEEK_USAGE),
                    "system_fingerprint": "fp_test",
                }
            ).encode()
            _reply(self, 200, encoded, "application/json")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _serve(Handler)
    try:
        gateway = _gateway(f"http://127.0.0.1:{server.server_address[1]}/v1")
        body = gateway.relay_chat(
            _JOB,
            {
                "model": "coder",
                "messages": [{"role": "user", "content": "edit app.py"}],
                "stream": True,
                "stream_options": {"include_usage": False},
            },
        )
    finally:
        server.shutdown()
        server.server_close()
    forwarded = seen["body"]
    assert isinstance(forwarded, dict)
    assert forwarded["model"] == "deepseek-chat"
    assert forwarded["stream"] is True
    assert forwarded["stream_options"] == {"include_usage": True}
    assert "messages" in forwarded
    assert body["model"] == "deepseek-chat"
    assert body["usage"]["prompt_tokens"] == 18
    assert body["usage"]["completion_tokens"] == 9
    assert body["usage"]["prompt_cache_hit_tokens"] == 0
    assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": 0}
    assert body["system_fingerprint"] == "fp_test"


def test_deepseek_json_and_sse_usage_are_metered_without_logging_the_body(monkeypatch, caplog) -> None:
    caplog.set_level("INFO", logger="aether.gateway.model")
    secret = "edit-the-file-marker"
    json_body = {
        "id": "chatcmpl-deepseek",
        "object": "chat.completion",
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "file_editor", "arguments": '{"command":"' + secret + '"}'},
                        }
                    ],
                },
            }
        ],
        "usage": dict(_DEEPSEEK_USAGE),
        "system_fingerprint": "fp_test",
    }

    class JsonHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            self.rfile.read(length)
            _reply(self, 200, json.dumps(json_body).encode(), "application/json")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _serve(JsonHandler)
    try:
        gateway = _gateway(f"http://127.0.0.1:{server.server_address[1]}/v1")
        parsed = gateway.relay_chat(_JOB, {"model": "coder", "messages": [{"role": "user", "content": secret}]})
    finally:
        server.shutdown()
        server.server_close()
    assert parsed["usage"]["prompt_tokens"] == 18
    assert parsed["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "file_editor"
    assert secret not in caplog.text
    assert _KEY not in caplog.text

    per_chunk = _sse(
        [
            {
                "id": "chatcmpl-ds",
                "model": "deepseek-chat",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hel"}, "finish_reason": None}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            {
                "id": "chatcmpl-ds",
                "model": "deepseek-chat",
                "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        ]
    )
    final_only = _sse(
        [
            {
                "id": "chatcmpl-ds",
                "object": "chat.completion.chunk",
                "model": "deepseek-chat",
                "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "file_editor", "arguments": ""}}]}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl-ds",
                "model": "deepseek-chat",
                "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"command":"' + secret + '"}'}}]}, "finish_reason": "tool_calls"}],
            },
            {
                "id": "chatcmpl-ds",
                "model": "deepseek-chat",
                "choices": [],
                "usage": dict(_DEEPSEEK_USAGE),
            },
        ]
    )
    streamed = {"per_chunk": per_chunk, "final_only": final_only}
    chosen = {"name": "per_chunk"}

    class StreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            self.rfile.read(length)
            _reply(self, 200, streamed[str(chosen["name"])], "text/event-stream")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _serve(StreamHandler)
    try:
        gateway = _gateway(f"http://127.0.0.1:{server.server_address[1]}/v1")
        chunked = gateway.relay_chat(_JOB, {"model": "openai/coder", "messages": [{"role": "user", "content": "one"}], "stream": True})
        chosen["name"] = "final_only"
        final = gateway.relay_chat(_JOB, {"model": "coder", "messages": [{"role": "user", "content": "two"}], "stream": True})
    finally:
        server.shutdown()
        server.server_close()
    assert chunked["model"] == "deepseek-chat"
    assert chunked["choices"][0]["message"]["content"] == "hello"
    assert chunked["usage"]["prompt_tokens"] == 4
    assert chunked["usage"]["completion_tokens"] == 2
    assert final["usage"]["prompt_tokens"] == 18
    assert final["usage"]["completion_tokens"] == 9
    assert final["usage"]["prompt_cache_miss_tokens"] == 18
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"command":"' + secret + '"}'
    assert secret not in caplog.text
    assert gateway._response_kind == "sse"


def test_upstream_http_errors_keep_the_status_and_drop_the_message(caplog) -> None:
    caplog.set_level("INFO", logger="aether.gateway.model")
    noted: list[tuple[str, int | None, str | None, bool]] = []
    message = "Model Not Exist"

    class Denied(BaseHTTPRequestHandler):
        status = 400
        body = b""

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            self.rfile.read(length)
            _reply(self, Denied.status, Denied.body, "application/json")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    Denied.body = json.dumps(
        {"error": {"message": message, "type": "invalid_request_error", "code": "invalid_request_error"}}
    ).encode()
    server = _serve(Denied)
    try:
        gateway = _gateway(f"http://127.0.0.1:{server.server_address[1]}/v1")
        gateway._note_upstream_failure = lambda _session, exc, *, release_pending: noted.append(  # type: ignore[method-assign]
            (exc.code, exc.upstream_status, exc.provider_error, release_pending)
        )
        try:
            gateway.relay_chat(_JOB, {"model": "coder", "messages": [{"role": "user", "content": message}]})
        except GatewayDenied as exc:
            assert exc.code == "upstream_http_400"
            assert exc.sent is True
            assert exc.upstream_status == 400
            assert exc.provider_error == "invalid_request_error"
            assert message not in exc.code
            assert message not in str(exc)
        else:
            raise AssertionError("400 was accepted")
        Denied.status = 500
        Denied.body = json.dumps({"error": {"message": "upstream exploded", "type": "server_error"}}).encode()
        try:
            gateway.relay_chat(_JOB, {"model": "coder", "messages": [{"role": "user", "content": "five"}]})
        except GatewayDenied as exc:
            assert exc.code == "upstream_http_500"
            assert exc.sent is True
            assert exc.upstream_status == 500
            assert exc.provider_error == "server_error"
            assert "exploded" not in exc.code
        else:
            raise AssertionError("500 was accepted")
    finally:
        server.shutdown()
        server.server_close()
    assert noted == [
        ("upstream_http_400", 400, "invalid_request_error", True),
        ("upstream_http_500", 500, "server_error", True),
    ]
    assert message not in caplog.text
    assert "exploded" not in caplog.text
    assert _KEY not in caplog.text


def test_gateway_process_receives_the_live_alias_and_not_the_provider_key(monkeypatch) -> None:
    monkeypatch.setenv("AETHER_LIVE_MODEL_ALIAS", "deepseek-chat")
    monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", "provider-secret")
    monkeypatch.setenv("AETHER_LIVE_MODEL_BASE", "https://api.deepseek.com/v1")
    env = _gateway_env("loop-token")
    assert env["AETHER_LIVE_MODEL_ALIAS"] == "deepseek-chat"
    assert env["AETHER_LIVE_MODEL_BASE"] == "https://api.deepseek.com/v1"
    assert "AETHER_LIVE_MODEL_KEY" not in env
    assert "provider-secret" not in "".join(env.values())
    monkeypatch.delenv("AETHER_LIVE_MODEL_ALIAS")
    bare = _gateway_env("loop-token")
    assert "AETHER_LIVE_MODEL_ALIAS" not in bare
