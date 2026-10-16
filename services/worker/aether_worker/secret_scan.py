"""Redact traces with detect-secrets. Heuristic filters are off so short fixtures stay visible."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.core.potential_secret import PotentialSecret
from detect_secrets.core.scan import scan_line
from detect_secrets.settings import get_plugins, transient_settings

_PLUGIN = Path(__file__).with_name("trace_secret_plugin.py").resolve()


def plugin_config() -> dict[str, object]:
    """Builtin AWS detector plus the trace plugin loaded by file path."""
    return {
        "plugins_used": [
            {"name": "TraceSecretDetector", "path": f"file://{_PLUGIN.as_posix()}"},
            {"name": "AWSKeyDetector"},
        ],
        "filters_used": [],
    }


@contextmanager
def scanning_settings() -> Iterator[None]:
    with transient_settings(plugin_config()):
        get_mapping_from_secret_type_to_class.cache_clear()
        get_plugins.cache_clear()
        yield


def scan_text(text: str) -> list[PotentialSecret]:
    with scanning_settings():
        return list(scan_line(text))


def redact_trace(text: str) -> str:
    values = sorted(
        {secret.secret_value for secret in scan_text(text) if secret.secret_value},
        key=len,
        reverse=True,
    )
    redacted = text
    for value in values:
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted
