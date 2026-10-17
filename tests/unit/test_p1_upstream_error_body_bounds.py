"""A non-2xx upstream answer's error body is read under the same deadline and byte cap as a 2xx body."""

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
_ERROR = {"error": {"code": "overloaded", "message": "try later"}}


class _FailingUpstream:
    """Answers 500; mode picks how the error body arrives. Counters and events replace sleeps."""

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

    def _write(self, handler: BaseHTTPRequestHandler, raw: bytes) -> bool:
        try:
            handler.wfile.write(raw)
            handler.wfile.flush()
        except OSError:
            return False
        return True

    def answer(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(500)
        handler.send_header("content-type", "application/json")
        if self.mode == "small":
            raw = json.dumps(_ERROR).encode()
            handler.send_header("content-length", str(len(raw)))
            handler.end_headers()
            self._write(handler, raw)
            return
        if self.mode == "big":
            raw = json.dumps({**_ERROR, "pad": "x" * self.size}).encode()
            handler.send_header("content-length", str(len(raw)))
            handler.end_headers()
            for start in range(0, len(raw), 16384):
                if self.stop.is_set() or not self._write(handler, raw[start : start + 16384]):
                    return
            return
        handler.end_headers()
        if self.mode == "endless":
            # No declared length: only the byte cap can end it.
            while not self.stop.is_set() and self._write(handler, b"x" * 4000):
                self.drips += 1
            return
        if self.mode == "drip":
            # One byte at a time, each well inside the per-read timeout, forever.
            while not self.stop.is_set() and self._write(handler, b" "):
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
    opened: list[_FailingUpstream] = []

    def make(mode: str, **kwargs: Any) -> _FailingUpstream:
        item = _FailingUpstream(mode, **kwargs)
        opened.append(item)
        return item

    yield make
    for item in opened:
        item.close()


def _gateway(base: str, *, timeout: float = 5) -> ModelGateway:
    return ModelGateway(cast(Database, object()), upstream_base=base, provider_key=_KEY, timeout=timeout)


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


def _assert_cut(error: BaseException | None) -> None:
    assert isinstance(error, GatewayDenied), error
    assert error.code == "upstream_error"
    assert error.sent is True
    assert error.upstream_status == 500


def test_a_dripping_error_body_is_cut_at_the_overall_deadline(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "0.5")
    upstream = upstreams("drip")
    error, elapsed, finished = _post_within(_gateway(upstream.base, timeout=5), 10)
    assert finished, f"still reading the error body after 10 s; {upstream.drips} drips"
    assert upstream.first_drip.is_set()
    _assert_cut(error)
    assert elapsed < 5


def test_an_error_body_declared_larger_than_the_cap_is_an_upstream_error(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "65536")
    upstream = upstreams("big", size=1_000_000)
    error, _elapsed, finished = _post_within(_gateway(upstream.base), 10)
    assert finished
    _assert_cut(error)


def test_an_endless_error_body_is_stopped_by_the_byte_cap(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "200000")
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "30")
    upstream = upstreams("endless")
    error, _elapsed, finished = _post_within(_gateway(upstream.base), 10)
    assert finished, f"still reading the error body after 10 s; {upstream.drips} chunks"
    _assert_cut(error)


def test_a_small_error_body_still_reports_the_http_status_and_provider_code(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "65536")
    monkeypatch.setenv("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "10")
    upstream = upstreams("small")
    with pytest.raises(GatewayDenied) as caught:
        _gateway(upstream.base)._post_upstream(dict(_BODY))
    assert caught.value.code == "upstream_http_500"
    assert caught.value.sent is True
    assert caught.value.upstream_status == 500
    assert caught.value.provider_error == "overloaded"


def test_a_cut_error_body_is_settled_through_the_upstream_failure_path(monkeypatch: pytest.MonkeyPatch, upstreams: Any) -> None:
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
    assert failures == [("upstream_error", True, 500, True)]
