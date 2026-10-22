"""SSE from the model upstream is read as the format defines it (r11 GPT review: CR-only line ends and
a leading BOM were not handled). Lines end in CRLF, LF or a lone CR; a leading UTF-8 BOM is not part of
the first field; a data field may span several data: lines, joined with a newline. Both readers, the
gateway's (parse_upstream_chat) and the Hermes comparison's (_meter_body), see the same events."""

from __future__ import annotations

import json

import pytest
from aether_gateway.model import parse_upstream_chat
from aether_worker.hermes_compare import _meter_body

_FIRST = {
    "id": "chatcmpl-1",
    "model": "deepseek-chat",
    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "def value():"}, "finish_reason": None}],
}
_LAST = {
    "id": "chatcmpl-1",
    "model": "deepseek-chat",
    "choices": [{"index": 0, "delta": {"content": " return 42"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 5},
}
_BOM = b"\xef\xbb\xbf"


def _event(payload: object, newline: str) -> str:
    return "data: " + json.dumps(payload, separators=(",", ":")) + newline + newline


def _multiline(payload: object, newline: str) -> str:
    # One JSON object split over two data: lines (the field value is the lines joined by "\n").
    text = json.dumps(payload, indent=1)
    head, tail = text.split("\n", 1)
    return "data: " + head + newline + "data: " + tail.replace("\n", " ") + newline + newline


def _stream(newline: str, *, multiline: bool = False) -> bytes:
    first = _multiline(_FIRST, newline) if multiline else _event(_FIRST, newline)
    return (first + _event(_LAST, newline) + "data: [DONE]" + newline + newline).encode()


_CASES = {
    "lf": _stream("\n"),
    "crlf": _stream("\r\n"),
    "cr": _stream("\r"),
    "bom_lf": _BOM + _stream("\n"),
    "bom_cr": _BOM + _stream("\r"),
    "multiline_lf": _stream("\n", multiline=True),
    "multiline_crlf": _stream("\r\n", multiline=True),
    "multiline_cr": _stream("\r", multiline=True),
}


@pytest.mark.parametrize("content_type", ["text/event-stream", ""])
@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_gateway_reads_every_sse_line_ending_a_bom_and_multiline_data(name: str, content_type: str) -> None:
    reply = parse_upstream_chat(_CASES[name], content_type)
    choice = reply["choices"][0]
    assert (choice["finish_reason"], choice["message"]["content"]) == ("stop", "def value(): return 42"), reply
    assert reply["id"] == "chatcmpl-1" and reply["model"] == "deepseek-chat", reply
    assert reply["usage"] == {"prompt_tokens": 7, "completion_tokens": 5}, reply


@pytest.mark.parametrize("content_type", ["text/event-stream", ""])
@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_hermes_comparison_reads_every_sse_line_ending_a_bom_and_multiline_data(name: str, content_type: str) -> None:
    content, usage, model, upstream_id = _meter_body(_CASES[name], content_type)
    assert (content, model, upstream_id) == ("def value(): return 42", "deepseek-chat", "chatcmpl-1"), usage
    assert usage == _meter_body(_CASES["lf"], "text/event-stream")[1], usage


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_an_event_with_an_event_field_but_no_data_is_skipped_by_both_readers(newline: str) -> None:
    # A named event (event: ping) with no data: line carries nothing; both readers skip it, the stream
    # around it is read in full, and it is never turned into an empty chunk.
    ping = "event: ping" + newline + newline
    stream = (ping + _event(_FIRST, newline) + ping + _event(_LAST, newline) + "event: done" + newline + newline).encode()
    reply = parse_upstream_chat(stream, "text/event-stream")
    assert reply["choices"][0]["message"]["content"] == "def value(): return 42", reply
    assert reply["usage"] == {"prompt_tokens": 7, "completion_tokens": 5}, reply
    content, usage, model, upstream_id = _meter_body(stream, "text/event-stream")
    assert (content, model, upstream_id) == ("def value(): return 42", "deepseek-chat", "chatcmpl-1"), usage
    assert usage == _meter_body(_CASES["lf"], "text/event-stream")[1], usage
