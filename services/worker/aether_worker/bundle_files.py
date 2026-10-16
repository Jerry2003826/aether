"""Shared path rules for candidate and dataset file maps."""

from __future__ import annotations

import json
from typing import Any


class BundleError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_object(data: bytes) -> dict[str, Any]:
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError("bundle_invalid") from exc
    if not isinstance(body, dict):
        raise BundleError("bundle_invalid")
    return body


def parse_file_map(value: object, *, limit: int = 32, allow_empty: bool = False) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > limit or (not value and not allow_empty):
        raise BundleError("bundle_invalid")
    parsed: dict[str, str] = {}
    for name, content in value.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise BundleError("path_rejected")
        try:
            encoded = content.encode("utf-8")
            name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BundleError("bundle_invalid") from exc
        parsed[safe_relative(name)] = content
        if len(encoded) > 256 * 1024:
            raise BundleError("bundle_invalid")
    return parsed


def safe_relative(name: str) -> str:
    if not name or name.startswith(("/", "\\")) or "\\" in name or "\x00" in name:
        raise BundleError("path_rejected")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BundleError("path_rejected")
    if not name.endswith(".py"):
        raise BundleError("path_rejected")
    return name
