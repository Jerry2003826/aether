"""redact_trace masks bearer tokens (Authorization headers and their lower-case forms)."""

from __future__ import annotations

import pytest
from aether_worker import coding_sandbox
from aether_worker.secret_scan import redact_trace

# Readable fixture phrase, not a credential.
_FIXTURE_TOKEN = "this-is-a-fixture-bearer-token"


@pytest.mark.parametrize(
    "line",
    [
        f"Authorization: Bearer {_FIXTURE_TOKEN}",
        f"authorization: bearer {_FIXTURE_TOKEN}",
        f"request failed headers={{'Authorization': 'Bearer {_FIXTURE_TOKEN}'}}",
        f"BEARER {_FIXTURE_TOKEN}",
    ],
)
def test_redact_trace_masks_a_bearer_token(line: str) -> None:
    redacted = redact_trace(line)
    assert _FIXTURE_TOKEN not in redacted
    assert "[REDACTED]" in redacted


def test_the_word_bearer_alone_is_kept() -> None:
    assert redact_trace("the bearer of this note") == "the bearer of this note"


def test_stored_log_tails_mask_bearer_tokens() -> None:
    logs = coding_sandbox._log_tails({"stdout": "", "stderr": f"before\nAuthorization: Bearer {_FIXTURE_TOKEN}\nafter\n"})
    assert _FIXTURE_TOKEN not in logs["stderr"] and logs["stderr"].endswith("after\n")
