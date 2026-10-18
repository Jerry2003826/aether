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


# Armor lines (RSA, EC, OPENSSH, ENCRYPTED, FOO-BAR, ... PRIVATE KEY). A label word may hold single
# hyphens between its characters (RFC 7468), never two in a row. Words, hyphens and spaces each have
# one place in the pattern, so a match attempt reads its run once and stops at the first character
# that fits nowhere (no backtracking over the rest of the text: linear time).
_LABEL = r" +(?:[A-Z0-9_]+(?:-[A-Z0-9_]+)* +)*"
_KEY_BEGIN = re.compile(r"-----BEGIN" + _LABEL + r"PRIVATE KEY-----")
_KEY_END = re.compile(r"-----END" + _LABEL + r"PRIVATE KEY-----")
_LINE_BREAK = re.compile(r"\r?\n")
_REDACTED_KEY = "[REDACTED PRIVATE KEY]"


def _mask_key_blocks(text: str) -> str:
    """Each BEGIN line through the first END line after it (or the end of the text) becomes one mask."""
    parts: list[str] = []
    position = 0
    while (begin := _KEY_BEGIN.search(text, position)) is not None:
        parts += [text[position : begin.start()], _REDACTED_KEY]
        end = _KEY_END.search(text, begin.end())
        if end is None:
            return "".join(parts)
        position = end.end()
    parts.append(text[position:])
    return "".join(parts)


def redact_text(text: str) -> str:
    """redact_trace over a whole multi-line text, such as a log tail.

    Private-key blocks are masked whole, also when the text ends inside one or starts inside one
    (an END line with no BEGIN before it: everything up to it goes). A secret wrapped across lines
    is found in a copy with the line breaks removed and masked across them in the original.
    """
    redacted = _mask_key_blocks(text)
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
