"""Local HTTP between the worker and the processes that hold privileged credentials."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class LoopbackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# An empty proxy map ignores HTTP_PROXY and does not forward loopback tokens.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _object(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    loaded = json.loads(raw.decode())
    if not isinstance(loaded, dict):
        raise LoopbackError("body_rejected")
    return loaded


def post_json(url: str, token: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return _object(response.read())
    except urllib.error.HTTPError as exc:
        body = _object(exc.read())
        if exc.code == 409 and body.get("denied") is True:
            return body
        raise LoopbackError(f"http_{exc.code}") from None
    except (TimeoutError, urllib.error.URLError) as exc:
        raise LoopbackError("unreachable") from exc


def get_json(url: str, *, timeout: float) -> dict[str, Any]:
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            return _object(response.read())
    except urllib.error.HTTPError as exc:
        raise LoopbackError(f"http_{exc.code}") from None
    except (TimeoutError, urllib.error.URLError) as exc:
        raise LoopbackError("unreachable") from exc
