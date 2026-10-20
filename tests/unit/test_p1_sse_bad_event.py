"""One bad SSE event fails the whole upstream answer; a partial stream is never a normal completion.

A `data:` event whose payload is not a JSON object (broken or cut-off JSON, or a bare JSON value)
used to be dropped as long as some other event parsed, so the relay stitched the rest into a
completion and returned it as finished. It must settle as upstream_error (sent=True, no upstream
status), the same path as an unreadable JSON body: the call's hold is then charged at settlement as
an unknown-usage call. Lines that carry no event data under the SSE format (comments, event:, id:,
retry:, blank lines, an empty data field, the [DONE] marker) are not bad events.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, cast

import pytest
from aether_gateway.model import GatewayDenied, ModelGateway, parse_upstream_chat
from aether_storage.db import Database

_KEY = "unit-provider-key"
_JOB = "job-token-sse"

_FIRST = {
    "id": "chatcmpl-1",
    "model": "deepseek-chat",
    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "def value():"}, "finish_reason": None}],
}
_LAST = {
    "id": "chatcmpl-1",
    "model": "deepseek-chat",
    "choices": [{"index": 0, "delta": {"content": " return 42"}, "finish_reason": "stop"}],
}
_USAGE = {"id": "chatcmpl-1", "model": "deepseek-chat", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 5}}


def _data(event: object) -> str:
    return "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"


_BAD_STREAMS = {
    "broken_json_between_valid_events": (
        _data(_FIRST) + 'data: {"choices":[{"index":0,"delta":{"content":" lost"\n\n' + _data(_LAST) + _data(_USAGE) + "data: [DONE]\n\n"
    ),
    "cut_off_final_event": _data(_FIRST) + _data(_LAST) + 'data: {"id":"chatcmpl-1","choices":[],"usage":{"prompt_tok',
    "json_value_that_is_not_an_object": _data(_FIRST) + "data: 42\n\n" + _data(_LAST) + _data(_USAGE) + "data: [DONE]\n\n",
}


@pytest.mark.parametrize("name", sorted(_BAD_STREAMS))
def test_a_bad_event_among_valid_ones_is_an_upstream_error(name: str) -> None:
    with pytest.raises(GatewayDenied) as denied:
        parse_upstream_chat(_BAD_STREAMS[name].encode(), "text/event-stream")
    assert denied.value.code == "upstream_error"
    assert denied.value.sent is True
    assert denied.value.upstream_status is None


def test_sse_lines_that_carry_no_event_data_are_not_bad_events() -> None:
    raw = (
        ": keep-alive comment\n\n"
        "event: message\nid: 1\nretry: 1000\n" + _data(_FIRST) + "\n"
        "data:\n\n"
        ":\r\n\r\n" + _data(_LAST).replace("\n", "\r\n") + _data(_USAGE) + "data: [DONE]\n\n"
    ).encode()
    parsed = parse_upstream_chat(raw, "text/event-stream")
    assert parsed["choices"][0]["message"]["content"] == "def value(): return 42"
    assert parsed["choices"][0]["finish_reason"] == "stop"
    assert parsed["usage"] == {"prompt_tokens": 7, "completion_tokens": 5}


class _Upstream:
    def __init__(self, payload: bytes) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or "0"))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _grant() -> dict[str, Any]:
    return {
        "id": "1",
        "project_id": "p",
        "task_id": "t",
        "scope": {"task_id": "t", "request_key": "coding-1", "attempt_id": "1", "capabilities": ["coding:model"], "aliases": ["coder"]},
    }


def test_the_relay_settles_a_stream_with_a_bad_event_as_an_upstream_error() -> None:
    upstream = _Upstream(_BAD_STREAMS["broken_json_between_valid_events"].encode())
    measured: list[dict[str, Any]] = []
    failures: list[tuple[GatewayDenied, bool]] = []
    try:
        gateway = ModelGateway(cast(Database, object()), upstream_base=upstream.base, provider_key=_KEY, timeout=5)
        gateway._scope = lambda token: _grant()  # type: ignore[method-assign]
        gateway._spend_coding_ticket = lambda _session, *, alias, prompt: 64  # type: ignore[method-assign]
        gateway._add_measured_usage = lambda _session, result: measured.append(result)  # type: ignore[method-assign]
        gateway._note_upstream_failure = lambda _session, exc, *, release_pending: failures.append((exc, release_pending))  # type: ignore[method-assign]
        with pytest.raises(GatewayDenied) as denied:
            gateway.relay_chat(_JOB, {"model": "coder", "messages": [{"role": "user", "content": "edit"}], "stream": True})
    finally:
        upstream.close()
    assert denied.value.code == "upstream_error"
    # No partial completion is metered as a known-usage answer.
    assert measured == []
    # The existing failure path: pending released, and sent with no upstream status, which
    # _note_upstream_failure counts as an unknown-usage call charged at its reservation.
    assert len(failures) == 1
    exc, release_pending = failures[0]
    assert release_pending is True
    assert (exc.code, exc.sent, exc.upstream_status) == ("upstream_error", True, None)
