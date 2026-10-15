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
    texts: list[str] = []
    for node in root.iter():
        if node.tag.endswith("title") or node.tag.endswith("description"):
            if node.text and node.text.strip():
                texts.append(node.text.strip())
    sys.stdout.write(json.dumps({"ok": True, "texts": texts, "pid": os.getpid()}))


if __name__ == "__main__":
    main()
