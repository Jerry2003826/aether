"""RSS parser child. Limits are applied before any document is read."""

from __future__ import annotations

import json
import os
import resource
import sys

import defusedxml.ElementTree as ET


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
        root = ET.fromstring(payload)
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": type(exc).__name__}))
        return
    texts = _arxiv_texts(root) if os.environ.get("AETHER_FEED_MODE") == "arxiv" else _rss_texts(root)
    sys.stdout.write(json.dumps({"ok": True, "texts": texts, "pid": os.getpid()}))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _plain(node: ET.Element) -> str:
    if node.text and node.text.strip():
        return node.text.strip()
    return ""


def _rss_texts(root: ET.Element) -> list[str]:
    texts: list[str] = []
    for node in root.iter():
        if node.tag.endswith("title") or node.tag.endswith("description"):
            plain = _plain(node)
            if plain:
                texts.append(plain)
    return texts


def _arxiv_texts(root: ET.Element) -> list[str]:
    texts: list[str] = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        title = ""
        summary = ""
        authors: list[str] = []
        for child in list(entry):
            name = _local(child.tag)
            if name == "title":
                title = _plain(child)
            elif name == "summary":
                summary = _plain(child)
            elif name == "author":
                authors.extend(_plain(part) for part in list(child) if _local(part.tag) == "name" and _plain(part))
        if title:
            texts.append(title)
        if summary:
            texts.append(summary)
        texts.extend(authors)
    return texts


if __name__ == "__main__":
    main()
