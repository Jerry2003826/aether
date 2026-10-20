"""secret_scan.redact_text takes time in proportion to its input. Armor-like text that never forms a
key block (many BEGIN or END fragments) must not make the private-key patterns backtrack over the
rest of the text from every fragment (Sonar python:S8786 on secret_scan.py)."""

from __future__ import annotations

import time

import pytest
from aether_worker.secret_scan import redact_text

# Readable fixture phrases; none of them is a whole armor line.
_SIZE = 512 * 1024
_BEGIN_FRAGMENT = "-----BEGIN NOT A KEY"
_END_FRAGMENT = "-----END NOT A KEY"
_OPEN_BLOCK = "-----BEGIN " + "PRIVATE KEY-----\n"
_HYPHEN_BEGIN = "-----BEGIN NOT-A KEY "
_HYPHEN_END = "-----END NOT-A KEY "
# Old patterns: over 10 s for each of these on the development box; fixed, under half a second.
_BOUND_SECONDS = 2.0


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_BEGIN_FRAGMENT * (_SIZE // len(_BEGIN_FRAGMENT)), id="begin-fragments"),
        pytest.param(_END_FRAGMENT * (_SIZE // len(_END_FRAGMENT)), id="end-fragments"),
        pytest.param(_OPEN_BLOCK + _END_FRAGMENT * (_SIZE // len(_END_FRAGMENT)), id="open-block-then-end-fragments"),
        pytest.param("-----BEGIN " + "A-" * (_SIZE // 2), id="begin-then-one-long-hyphen-label"),
        pytest.param("-----BEGIN " + "A " * (_SIZE // 2), id="begin-then-many-label-words"),
        pytest.param(_HYPHEN_BEGIN * (_SIZE // len(_HYPHEN_BEGIN)), id="hyphen-label-begin-fragments"),
        pytest.param(_OPEN_BLOCK + _HYPHEN_END * (_SIZE // len(_HYPHEN_END)), id="open-block-then-hyphen-label-end-fragments"),
    ],
)
def test_redaction_of_armor_fragments_stays_linear(text: str) -> None:
    started = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - started < _BOUND_SECONDS


def test_key_blocks_are_still_masked_whole() -> None:
    body = "fixture key body line"
    for label in ("", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "):
        text = "before\n-----BEGIN " + label + "PRIVATE KEY-----\n" + body + "\n-----END " + label + "PRIVATE KEY-----\nafter\n"
        stored = redact_text(text)
        assert body not in stored, label
        assert stored.startswith("before\n") and stored.endswith("after\n"), label
