"""RSS parser child. Limits are applied before any document is read."""

from __future__ import annotations

import json
import os
import resource
import sys

import feedparser

_ENTITY_MARKERS = ("<!doctype", "<!entity")
_WIDE_ENCODINGS = ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be")


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except (ValueError, OSError):
        pass
    if os.environ.get("AETHER_PARSER_SPIN") == "1":
        while True:
            pass
    payload = sys.stdin.buffer.read()
    try:
        texts = parse_feed(payload, mode=os.environ.get("AETHER_FEED_MODE") or "rss")
    except ValueError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": exc.args[0] if exc.args else type(exc).__name__}))
        return
    sys.stdout.write(json.dumps({"ok": True, "texts": texts, "pid": os.getpid()}))


def parse_feed(payload: bytes, *, mode: str) -> list[str]:
    """Parse with feedparser. Custom entities are refused before the library expands them."""
    if _declares_entity(payload):
        raise ValueError("entity_rejected")
    parsed = feedparser.parse(payload)
    texts = _arxiv_texts(parsed) if mode == "arxiv" else _rss_texts(parsed)
    if parsed.get("bozo") and not texts:
        raise ValueError("parse_rejected")
    return texts


def _declares_entity(payload: bytes) -> bool:
    """Refuse a DTD before feedparser expands it. UTF-16 and UTF-32 hide the ASCII bytes behind NULs."""
    for text in _xml_texts(payload):
        lowered = text.lower()
        if any(marker in lowered for marker in _ENTITY_MARKERS):
            return True
    return False


def _xml_texts(payload: bytes) -> list[str]:
    encodings: list[str] = []
    detected = _xml_encoding(payload)
    if detected is not None:
        encodings.append(detected)
    encodings.append("utf-8")
    if b"\x00" in payload or detected in _WIDE_ENCODINGS:
        encodings.extend(_WIDE_ENCODINGS)
    texts: list[str] = []
    seen: set[str] = set()
    for encoding in encodings:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            texts.append(payload.decode(encoding, errors="replace"))
        except (UnicodeError, LookupError):
            continue
    return texts


def _xml_encoding(payload: bytes) -> str | None:
    if payload.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    if payload.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if payload.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if len(payload) >= 4 and payload[1] == 0 and payload[3] == 0 and payload[0] != 0:
        return "utf-16-le"
    if len(payload) >= 4 and payload[0] == 0 and payload[2] == 0 and payload[1] != 0:
        return "utf-16-be"
    if len(payload) >= 4 and payload[:3] == b"\x00\x00\x00":
        return "utf-32-be"
    if len(payload) >= 4 and payload[1:4] == b"\x00\x00\x00":
        return "utf-32-le"
    return None


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _rss_texts(parsed: feedparser.FeedParserDict) -> list[str]:
    texts: list[str] = []
    feed = parsed.get("feed") or {}
    for key in ("title", "subtitle", "description"):
        _append(texts, feed.get(key))
    for entry in parsed.get("entries") or []:
        _append(texts, entry.get("title"))
        summary = _clean(entry.get("summary"))
        description = _clean(entry.get("description"))
        _append(texts, summary or description)
        if description and description != summary:
            _append(texts, description)
    return texts


def _arxiv_texts(parsed: feedparser.FeedParserDict) -> list[str]:
    texts: list[str] = []
    for entry in parsed.get("entries") or []:
        title = _clean(entry.get("title"))
        if title:
            texts.append(title)
        _append(texts, entry.get("summary"))
        for author in entry.get("authors") or []:
            if isinstance(author, dict):
                _append(texts, author.get("name"))
    return texts


def _append(texts: list[str], value: object) -> None:
    plain = _clean(value)
    if plain:
        texts.append(plain)


if __name__ == "__main__":
    main()
