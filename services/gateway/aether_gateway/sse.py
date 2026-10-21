"""Server-sent events from a model upstream, read with httpx-sse after the body was read in full.

The caller has already read the whole body under its own byte cap and deadline; this only splits it
into events. Lines end in CRLF, LF or a lone CR (httpx-sse's line decoder), a leading UTF-8 BOM is
dropped as the SSE format requires, and a data field spanning several data: lines is joined with
"\\n". A final event with no blank line after it is still returned, so the caller sees a cut-off last
event and fails on it instead of silently dropping it.
"""

from __future__ import annotations

import httpx
from httpx_sse import EventSource

_BOM = b"\xef\xbb\xbf"
_EVENT_STREAM = "text/event-stream; charset=utf-8"


def without_bom(raw: bytes) -> bytes:
    return raw.removeprefix(_BOM)


def looks_like_event_stream(raw: bytes) -> bool:
    """A body without an event-stream content type that still starts with a data: field."""
    return without_bom(raw).lstrip().startswith(b"data:")


def data_fields(raw: bytes) -> list[str]:
    """The data of every event, in order; events without a data field are left out."""
    # The trailing blank line ends a last event the upstream left unterminated.
    response = httpx.Response(200, headers={"content-type": _EVENT_STREAM}, content=without_bom(raw) + b"\n\n")
    return [event.data for event in EventSource(response).iter_sse() if event.data]
