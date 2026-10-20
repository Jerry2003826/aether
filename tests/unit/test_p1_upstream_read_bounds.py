"""The coding relay reads one upstream response under one overall deadline and a byte cap."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest

from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.db import Database

_KEY = "unit-provider-key"
_BODY = {"model": "coder", "messages": [{"role": "user", "content": "edit"}]}
_ANSWER = {
    "id": "c1",
    "model": "coder-upstream",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
}


class _Upstream:
    """A local upstream. mode picks how it answers; counters and events replace sleeps."""

    def __init__(self, mode: str, *, size: int = 0) -> None:
        self.mode = mode
        self.size = size
        self.stop = threading.Event()
        self.first_drip = threading.Event()
        self.drips = 0
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or "0"))
                upstream.answer(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def answer(self, handler: BaseHTTPRequestHandler) -> None:
        if self.mode == "ok":
            raw = json.dumps(_ANSWER).encode()
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            return
        if self.mode == "big":
            filler = json.dumps({**_ANSWER, "pad": "x" * self.size}).encode()
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(filler)))
            handler.end_headers()
            for start in range(0, len(filler), 16384):
                if self.stop.is_set():
                    return
                try:
                    handler.wfile.write(filler[start : start + 16384])
                except OSError:
                    return
            return
        if self.mode == "big_stream":
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.end_headers()
            chunk = b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "y" * 4000}}]}).encode() + b"\n\n"
            while not self.stop.is_set():
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except OSError:
                    return
                self.drips += 1
            return
        if self.mode == "drip":
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.end_headers()
            # One byte at a time, each well inside the per-read timeout, forever.
            while not self.stop.is_set():
                try:
                    handler.wfile.write(b":")
                    handler.wfile.flush()
                except OSError:
                    return
                self.drips += 1
                self.first_drip.set()
                self.stop.wait(0.02)
            return
        raise AssertionError(self.mode)

    def close(self) -> None:
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstreams() -> Any:
    opened: list[_Upstream] = []

    def make(mode: str, **kwargs: Any) -> _Upstream:
        item = _Upstream(mode, **kwargs)
        opened.append(item)
        return item

    yield make
    for item in opened:
        item.close()


def _post_within(gateway: ModelGateway, limit: float) -> tuple[BaseException | None, float, bool]:
    """Run the read in a thread so a missing bound fails the test instead of hanging it."""
    outcome: dict[str, BaseException] = {}
    done = threading.Event()

    def run() -> None:
        try:
            gateway._post_upstream(dict(_BODY))
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            outcome["error"] = exc
        finally:
            done.set()

    started = time.monotonic()
    threading.Thread(target=run, daemon=True).start()
    finished = done.wait(limit)
    return outcome.get("error"), time.monotonic() - started, finished


def _gateway(base: str, *, timeout: float = 5) -> ModelGateway:
    return ModelGateway(cast(Database, object()), upstream_base=base, provider_key=_KEY, timeout=timeout)


def test_a_slow_drip_upstream_is_cut_at_one_overall_deadline(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "0.5")
    upstream = upstreams("drip")
    gateway = _gateway(upstream.base, timeout=5)
    error, elapsed, finished = _post_within(gateway, 10)
    # Each byte arrives well inside the 5 s per-read timeout; only the overall deadline can stop it.
    assert finished, f"still reading after 10 s; {upstream.drips} drips"
    assert upstream.first_drip.is_set()
    assert upstream.drips > 1
    assert isinstance(error, GatewayDenied)
    assert error.code == "upstream_error"
    assert error.sent is True
    assert error.upstream_status is None
    assert elapsed < 5


def test_a_response_larger_than_the_byte_cap_is_an_upstream_error(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "65536")
    upstream = upstreams("big", size=1_000_000)
    with pytest.raises(GatewayDenied) as caught:
        _gateway(upstream.base)._post_upstream(dict(_BODY))
    assert caught.value.code == "upstream_error"
    assert caught.value.sent is True
    assert caught.value.upstream_status is None


def test_an_endless_stream_is_stopped_by_the_byte_cap(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "200000")
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "30")
    upstream = upstreams("big_stream")
    error, _elapsed, finished = _post_within(_gateway(upstream.base), 10)
    assert finished, f"still reading after 10 s; {upstream.drips} chunks"
    assert isinstance(error, GatewayDenied)
    assert error.code == "upstream_error"
    assert error.sent is True


def test_a_normal_answer_under_both_bounds_is_returned(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "65536")
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "10")
    upstream = upstreams("ok")
    result = _gateway(upstream.base)._post_upstream(dict(_BODY))
    assert result["usage"] == {"prompt_tokens": 3, "completion_tokens": 2}
    assert result["choices"][0]["message"]["content"] == "ok"


def test_a_cut_response_is_settled_through_the_upstream_failure_path(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "65536")
    upstream = upstreams("big", size=1_000_000)
    gateway = _gateway(upstream.base)
    grant = {
        "id": "1",
        "project_id": "p",
        "task_id": "t",
        "scope": {"request_key": "coding-1", "attempt_id": "1", "capabilities": ["coding:model"], "aliases": ["coder"]},
    }
    failures: list[tuple[str, bool, int | None, bool]] = []
    gateway._scope = lambda _token: grant  # type: ignore[method-assign]
    gateway._spend_coding_ticket = lambda _session, *, alias, prompt: 64  # type: ignore[method-assign]
    gateway._add_measured_usage = lambda _session, _result: (_ for _ in ()).throw(AssertionError("usage booked"))  # type: ignore[method-assign]

    def failed(_session: object, exc: GatewayDenied, *, release_pending: bool) -> None:
        failures.append((exc.code, exc.sent, exc.upstream_status, release_pending))

    gateway._note_upstream_failure = failed  # type: ignore[method-assign]
    with pytest.raises(GatewayDenied):
        gateway.relay_chat("job", dict(_BODY))
    assert failures == [("upstream_error", True, None, True)]


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "nan", "inf", "1e400"])
def test_invalid_deadline_settings_fall_back_to_the_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    from aether_gateway.model import UPSTREAM_DEADLINE_SECONDS, upstream_deadline_seconds

    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", raw)
    assert upstream_deadline_seconds() == UPSTREAM_DEADLINE_SECONDS


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "1.5", "nan"])
def test_invalid_byte_cap_settings_fall_back_to_the_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    from aether_gateway.model import UPSTREAM_MAX_BYTES, upstream_max_bytes

    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", raw)
    assert upstream_max_bytes() == UPSTREAM_MAX_BYTES


def test_valid_settings_are_used_and_the_defaults_are_sane(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_gateway.model import UPSTREAM_DEADLINE_SECONDS, UPSTREAM_MAX_BYTES, upstream_deadline_seconds, upstream_max_bytes

    assert 30 <= UPSTREAM_DEADLINE_SECONDS <= 600
    assert 1_048_576 <= UPSTREAM_MAX_BYTES <= 64 * 1_048_576
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "12.5")
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "4096")
    assert upstream_deadline_seconds() == 12.5
    assert upstream_max_bytes() == 4096
