"""Redact traces with detect-secrets. Heuristic filters are off so short fixtures stay visible."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.core.potential_secret import PotentialSecret
from detect_secrets.core.scan import scan_line
from detect_secrets.settings import get_plugins, transient_settings

_PLUGIN = Path(__file__).with_name("trace_secret_plugin.py").resolve()


def plugin_config() -> dict[str, object]:
    """Builtin AWS, basic-auth and keyword detectors plus the trace plugin loaded by file path."""
    return {
        "plugins_used": [
            {"name": "TraceSecretDetector", "path": f"file://{_PLUGIN.as_posix()}"},
            {"name": "AWSKeyDetector"},
            # user:password@ in a connection URL, and quoted 'password': '...' / "password": "...".
            {"name": "BasicAuthDetector"},
            {"name": "KeywordDetector"},
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


_KEY_BLOCK = re.compile(r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----.*?(?:-----END[ A-Z0-9_-]*PRIVATE KEY-----|\Z)", re.DOTALL)
_KEY_END = re.compile(r"-----END[ A-Z0-9_-]*PRIVATE KEY-----")
_LINE_BREAK = re.compile(r"\r?\n")
_REDACTED_KEY = "[REDACTED PRIVATE KEY]"


def redact_text(text: str) -> str:
    """redact_trace over a whole multi-line text, such as a log tail.

    Private-key blocks are masked whole, also when the text ends inside one or starts inside one
    (an END line with no BEGIN before it: everything up to it goes). A secret wrapped across lines
    is found in a copy with the line breaks removed and masked across them in the original.
    """
    redacted = _KEY_BLOCK.sub(_REDACTED_KEY, text)
    orphan = _KEY_END.search(redacted)
    if orphan is not None:
        redacted = _REDACTED_KEY + redacted[orphan.end() :]
    redacted = redact_trace(redacted)
    joined = _LINE_BREAK.sub("", redacted)
    wrapped = sorted(
        {secret.secret_value for secret in scan_text(joined) if secret.secret_value and secret.secret_value not in redacted},
        key=len,
        reverse=True,
    )
    for value in wrapped:
        redacted = re.sub(r"(?:\r?\n)?".join(re.escape(character) for character in value), "[REDACTED]", redacted)
    return redacted
