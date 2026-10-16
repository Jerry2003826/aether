"""RSS parser child. Limits are applied before any document is read."""

from __future__ import annotations

import json
import os
import resource
import sys

import feedparser


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
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("entity_rejected")
    parsed = feedparser.parse(payload)
    texts = _arxiv_texts(parsed) if mode == "arxiv" else _rss_texts(parsed)
    if parsed.get("bozo") and not texts:
        raise ValueError("parse_rejected")
    return texts


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
