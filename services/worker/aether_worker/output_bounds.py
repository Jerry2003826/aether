"""Shared caps for container output the host reads back. Only the tail of each stream is kept.

The coding sandbox log readers (SDK and CLI fallback) and the foreground `docker run` in
container_runtime all use these, so one stream never costs more than its cap in host memory.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import BinaryIO

# stderr, and stdout that carries no result line.
LOG_CAPTURE_LIMIT = 65536
# The RESULT line carries the whole patch. The sandbox entry caps the patch at MAX_PATCH_BYTES,
# so the final line always fits inside this stdout window.
MAX_PATCH_BYTES = 2_097_152
RESULT_CAPTURE_LIMIT = 4 * MAX_PATCH_BYTES
_READ_SIZE = 8192


def tail_bytes(chunks: Iterable[bytes | str], limit: int) -> bytes:
    """Consume `chunks` and return only the last `limit` bytes; at most one chunk more is ever held."""
    if limit < 1:
        return b""
    kept: deque[bytes] = deque()
    size = 0
    for chunk in chunks:
        piece = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
        if not piece:
            continue
        kept.append(piece)
        size += len(piece)
        while kept and size - len(kept[0]) >= limit:
            size -= len(kept.popleft())
    joined = b"".join(kept)
    kept.clear()
    return joined[-limit:]


def read_bounded_tail(stream: BinaryIO, limit: int) -> str:
    """Keep only the last `limit` bytes. The host does not retain the rest."""
    if limit < 1:
        return ""
    return tail_bytes(iter(lambda: stream.read(_READ_SIZE), b""), limit).decode("utf-8", errors="replace")
