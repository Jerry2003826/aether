"""Live model probe. Missing configuration is blocked, never a scripted pass."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from typing import Any

LIVE_KEY = "AETHER_LIVE_MODEL_KEY"
LIVE_BASE = "AETHER_LIVE_MODEL_BASE"
LIVE_ALIAS = "AETHER_LIVE_MODEL_ALIAS"


def required_environment() -> list[str]:
    return [LIVE_KEY, LIVE_BASE, LIVE_ALIAS]


def run_live_probe() -> dict[str, Any]:
    missing = [name for name in required_environment() if not os.environ.get(name)]
    if missing:
        return {
            "status": "blocked",
            "live_ready": False,
            "missing": missing,
            "research": "not_run",
            "coding": "not_run",
            "scripted_substitute": False,
        }
    base = os.environ[LIVE_BASE].rstrip("/")
    alias = os.environ[LIVE_ALIAS]
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(
            {"model": alias, "messages": [{"role": "user", "content": "回复一个词：pong"}], "max_tokens": 16}
        ).encode(),
        headers={"Authorization": f"Bearer {os.environ[LIVE_KEY]}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            status = response.status
    except Exception as exc:
        return {
            "status": "failed",
            "live_ready": False,
            "missing": [],
            "research": "not_run",
            "coding": "not_run",
            "error": type(exc).__name__,
            "scripted_substitute": False,
        }
    return {
        "status": "probed" if status == 200 else "failed",
        "live_ready": False,
        "http_status": status,
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "research": "not_run",
        "coding": "not_run",
        "scripted_substitute": False,
        "missing": [],
    }
