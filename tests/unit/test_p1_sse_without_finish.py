"""A reply is complete only with a finish signal; without one it is incomplete, never a reply.

SSE: at least one event must carry a non-empty finish_reason. [DONE] alone, or the end of the
stream, is not a finish signal. JSON: choices[0].finish_reason must be a non-empty string. An
incomplete reply is GatewayDenied("upstream_incomplete", sent=True). It is charged by what came
back: a usage block before the cut is measured usage; none means the call's reservation.
finish_reason=length is complete (cut at max_tokens) and is still a reply.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, cast

import pytest
from aether_gateway.model import GatewayDenied, ModelGateway, parse_upstream_chat
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_jobs import _settle
from ledger_sql_fake import FakeLedgerDatabase, prices_at

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_PARENT = "coding-job-finish"
_CALL = 3000

_DELTA = {"id": "chatcmpl-1", "model": "deepseek-chat", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "def value():"}, "finish_reason": None}]}
_MORE = {"id": "chatcmpl-1", "model": "deepseek-chat", "choices": [{"index": 0, "delta": {"content": " return 42"}, "finish_reason": None}]}
_LAST = {"id": "chatcmpl-1", "model": "deepseek-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
_USAGE = {"id": "chatcmpl-1", "model": "deepseek-chat", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 5}}


def _data(event: object) -> str:
    return "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"


def _incomplete(raw: str, content_type: str) -> GatewayDenied:
    with pytest.raises(GatewayDenied) as denied:
        parse_upstream_chat(raw.encode(), content_type)
    return denied.value


def test_a_stream_that_ends_without_finish_reason_is_incomplete() -> None:
    denied = _incomplete(_data(_DELTA) + _data(_MORE), "text/event-stream")
    assert (denied.code, denied.sent, denied.upstream_status) == ("upstream_incomplete", True, None)


def test_done_without_finish_reason_is_still_incomplete() -> None:
    denied = _incomplete(_data(_DELTA) + _data(_MORE) + _data(_USAGE) + "data: [DONE]\n\n", "text/event-stream")
    assert (denied.code, denied.sent) == ("upstream_incomplete", True)


def test_an_empty_finish_reason_is_not_a_finish_signal() -> None:
    empty = {**_LAST, "choices": [{"index": 0, "delta": {}, "finish_reason": ""}]}
    denied = _incomplete(_data(_DELTA) + _data(empty) + "data: [DONE]\n\n", "text/event-stream")
    assert denied.code == "upstream_incomplete"


def test_a_stream_with_a_finish_reason_is_a_complete_reply() -> None:
    parsed = parse_upstream_chat((_data(_DELTA) + _data(_MORE) + _data(_LAST) + _data(_USAGE) + "data: [DONE]\n\n").encode(), "text/event-stream")
    assert parsed["choices"][0]["finish_reason"] == "stop"
    assert parsed["choices"][0]["message"]["content"] == "def value(): return 42"


def test_a_json_reply_without_finish_reason_is_incomplete() -> None:
    body = {"id": "c1", "model": "coder", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    denied = _incomplete(json.dumps(body), "application/json")
    assert (denied.code, denied.sent, denied.upstream_status) == ("upstream_incomplete", True, None)
    assert denied.usage == (3, 2)
    null = {**body, "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": None}]}
    assert _incomplete(json.dumps(null), "application/json").code == "upstream_incomplete"


def test_a_json_reply_cut_at_max_tokens_is_still_a_complete_reply() -> None:
    body = {"id": "c1", "model": "coder", "choices": [{"index": 0, "message": {"role": "assistant", "content": "par"}, "finish_reason": "length"}]}
    assert parse_upstream_chat(json.dumps(body).encode(), "application/json")["choices"][0]["finish_reason"] == "length"


class _Upstream:
    def __init__(self, payload: bytes, content_type: str) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or "0"))
                self.send_response(200)
                self.send_header("content-type", content_type)
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
        "project_id": _PROJECT,
        "task_id": _TASK,
        "scope": {"task_id": _TASK, "request_key": _PARENT, "attempt_id": "1", "capabilities": ["coding:model"], "aliases": ["coder"]},
    }


def _relay(payload: str, content_type: str) -> tuple[FakeLedgerDatabase, GatewayDenied]:
    """One answered call (100/50), then one relayed call whose reply has no finish signal; then settle."""
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=8192, tokens_held=8192, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=8192,
        usage_detail={"reserved_tokens": 8192, "call_reserve_tokens": 4096, "call_reserve_micro_usd": 4096, **prices_at(4096, 4096), "pending_tokens": 2 * _CALL},
    )
    upstream = _Upstream(payload.encode(), content_type)
    try:
        gateway = ModelGateway(cast(Database, db), upstream_base=upstream.base, provider_key="unit-key", timeout=5)
        gateway._coding_call_tokens = _CALL
        gateway._add_measured_usage(_grant(), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
        gateway._scope = lambda _token: _grant()  # type: ignore[method-assign]
        gateway._spend_coding_ticket = lambda _session, *, alias, prompt: 64  # type: ignore[method-assign]
        with pytest.raises(GatewayDenied) as denied:
            gateway.relay_chat("job-token-finish", {"model": "coder", "messages": [{"role": "user", "content": "edit"}], "stream": True})
    finally:
        upstream.close()
    _settle(BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None)
    return db, denied.value


def test_an_incomplete_stream_with_usage_is_charged_measured_usage() -> None:
    db, denied = _relay(_data(_DELTA) + _data(_MORE) + _data(_USAGE) + "data: [DONE]\n\n", "text/event-stream")
    assert (denied.code, denied.sent) == ("upstream_incomplete", True)
    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    assert row["usage_detail"]["upstream_error"] == "upstream_incomplete"
    assert "unknown_reserved_tokens" not in row["usage_detail"]
    assert row["usage_detail"]["pending_tokens"] == 0
    assert (row["input_tokens"], row["output_tokens"]) == (107, 55)
    assert account["spent_micro_usd"] == 150 + 12
    assert account["tokens_used"] == 150 + 12
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_an_incomplete_stream_without_usage_is_charged_at_the_reservation() -> None:
    db, denied = _relay(_data(_DELTA) + _data(_MORE) + "data: [DONE]\n\n", "text/event-stream")
    assert (denied.code, denied.sent) == ("upstream_incomplete", True)
    row = db.request(_PROJECT, _TASK, _PARENT)
    account = db.account(_PROJECT, _TASK)
    assert row["usage_detail"]["upstream_error"] == "upstream_incomplete"
    assert row["usage_detail"]["unknown_reserved_tokens"] == _CALL
    assert row["usage_detail"]["unknown_usage_calls"] == 1
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["tokens_used"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
