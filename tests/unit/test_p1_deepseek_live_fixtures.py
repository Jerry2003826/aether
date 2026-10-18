"""Real DeepSeek replies (tests/fixtures/deepseek_live, redacted) are accepted as complete answers.

The incomplete-reply rule (ADR-F46: no finish_reason means upstream_incomplete) is checked
against what the production provider really sends, one non-streaming and one streaming reply,
by parse_upstream_chat directly, by the gateway relay and through the job's model proxy.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from aether_gateway.model import ModelGateway, parse_upstream_chat
from aether_storage.db import Database
from aether_worker import coding_model_proxy
from aether_worker.coding_model_proxy import CodingModelProxy

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "deepseek_live"
_JSON = (_FIXTURES / "chat_nonstream.json").read_bytes()
_SSE = (_FIXTURES / "chat_stream.sse").read_bytes()
_JSON_TYPE = "application/json"
_SSE_TYPE = "text/event-stream; charset=utf-8"
_JOB = "job-token-live-fixture"
_PROVIDER = "provider-key-placeholder"
_REPLIES = {"json": (_JSON, _JSON_TYPE), "sse": (_SSE, _SSE_TYPE)}


def _grant() -> dict[str, Any]:
    scope = {"task_id": "t", "request_key": "coding-1", "attempt_id": "1", "capabilities": ["coding:model"], "aliases": ["coder"]}
    return {"id": "1", "project_id": "p", "task_id": "t", "scope": scope}


def _metered_gateway(cls: type[ModelGateway], booked: list[tuple[str, Any]]) -> type[ModelGateway]:
    """A gateway with the ledger replaced: it records what would be booked."""

    class Metered(cls):  # type: ignore[valid-type,misc]
        def _scope(self, token: str) -> dict[str, Any]:
            assert token == _JOB
            return _grant()

        def _spend_coding_ticket(self, _session: dict[str, Any], *, alias: str, prompt: Any) -> int:
            return 16

        def _add_measured_usage(self, _session: dict[str, Any], result: dict[str, Any]) -> None:
            booked.append(("measured", result.get("usage")))

        def _note_upstream_failure(self, _session: dict[str, Any], exc: Any, *, release_pending: bool) -> None:
            booked.append(("failure", exc.code))

    return Metered


class _Upstream:
    def __init__(self) -> None:
        self.reply = "json"
        self.forwarded: list[dict[str, Any]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                upstream.forwarded.append(json.loads(self.rfile.read(int(self.headers.get("content-length") or "0"))))
                payload, content_type = _REPLIES[upstream.reply]
                self.send_response(200)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    made = _Upstream()
    try:
        yield made
    finally:
        made.close()


def _assert_answer(parsed: dict[str, Any]) -> None:
    assert parsed["choices"][0]["finish_reason"] == "stop"
    assert parsed["choices"][0]["message"]["content"] == "ok"
    assert parsed["model"] == "deepseek-flash"
    assert parsed["usage"]["prompt_tokens"] == 11
    assert parsed["usage"]["completion_tokens"] == 1


@pytest.mark.parametrize("kind", ["json", "sse"])
def test_parse_upstream_chat_accepts_the_real_replies(kind: str) -> None:
    payload, content_type = _REPLIES[kind]
    _assert_answer(parse_upstream_chat(payload, content_type))


def test_the_real_stream_ends_with_its_finish_signal_and_usage_on_one_event() -> None:
    # Pin what the fixture shows, so the acceptance above is about this real shape.
    events = [json.loads(line[5:]) for line in _SSE.decode().splitlines() if line.startswith("data:") and line.strip() != "data: [DONE]"]
    assert [event["choices"][0]["finish_reason"] for event in events] == [None, None, "stop"]
    assert events[-1]["usage"]["completion_tokens"] == 1
    assert _SSE.rstrip().endswith(b"data: [DONE]")


@pytest.mark.parametrize("kind", ["json", "sse"])
def test_the_gateway_relay_meters_the_real_replies(monkeypatch: pytest.MonkeyPatch, upstream: _Upstream, kind: str) -> None:
    monkeypatch.setenv("AETHER_LIVE_MODEL_ALIAS", "deepseek-chat")
    upstream.reply = kind
    booked: list[tuple[str, Any]] = []
    gateway = _metered_gateway(ModelGateway, booked)(cast(Database, object()), upstream_base=upstream.base, provider_key=_PROVIDER, timeout=5)

    body: dict[str, Any] = {"model": "coder", "messages": [{"role": "user", "content": "Reply with exactly the word: ok"}]}
    if kind == "sse":
        body["stream"] = True
    parsed = gateway.relay_chat(_JOB, body)

    _assert_answer(parsed)
    assert booked == [("measured", parsed["usage"])]
    assert upstream.forwarded[0]["model"] == "deepseek-chat"


def _chat_over_socket(path: Path, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(body).encode()
    request = (
        f"POST /v1/chat/completions HTTP/1.1\r\nauthorization: Bearer {_JOB}\r\ncontent-type: application/json\r\n"
        f"content-length: {len(raw)}\r\nconnection: close\r\n\r\n"
    ).encode("ascii") + raw
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(path))
        sock.sendall(request)
        chunks = []
        while block := sock.recv(65536):
            chunks.append(block)
    head, _, payload = b"".join(chunks).partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), json.loads(payload)


@pytest.mark.parametrize("kind", ["json", "sse"])
def test_the_job_proxy_returns_the_real_replies_as_answers(monkeypatch: pytest.MonkeyPatch, upstream: _Upstream, kind: str) -> None:
    upstream.reply = kind
    booked: list[tuple[str, Any]] = []
    monkeypatch.delenv("AETHER_GATEWAY_BASE", raising=False)
    monkeypatch.delenv("AETHER_LOOPBACK_TOKEN", raising=False)
    monkeypatch.setenv("AETHER_LIVE_MODEL_BASE", upstream.base)
    monkeypatch.setenv("AETHER_LIVE_MODEL_ALIAS", "deepseek-chat")
    monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", _PROVIDER)
    monkeypatch.setattr(coding_model_proxy, "ModelGateway", _metered_gateway(ModelGateway, booked))
    proxy = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    try:
        body: dict[str, Any] = {"model": "coder", "messages": [{"role": "user", "content": "Reply with exactly the word: ok"}]}
        if kind == "sse":
            body["stream"] = True
        status, answer = _chat_over_socket(proxy.socket_path, body)
        assert proxy.drain() == 0
    finally:
        proxy.close()

    assert status == 200, answer
    _assert_answer(answer)
    assert booked == [("measured", answer["usage"])]
