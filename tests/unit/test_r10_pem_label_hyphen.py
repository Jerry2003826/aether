"""PEM labels may hold a hyphen between label characters (RFC 7468 labelchar *(["-" / SP] labelchar)):
a private-key block whose label has one is masked whole like any other, in redact_text and in the
stored sandbox log tails."""

from __future__ import annotations

import pytest
from aether_worker import coding_sandbox
from aether_worker.secret_scan import redact_text

# Readable fixture phrases, not credentials. The armor lines are assembled at run time.
_BODY = ["fixture key body line one", "fixture key body line two"]


def _block(label: str) -> str:
    return "\n".join(["-----BEGIN " + label + "PRIVATE KEY-----", *_BODY, "-----END " + label + "PRIVATE KEY-----"])


@pytest.mark.parametrize("label", ["FOO-BAR ", "X-Y-Z RSA ", "OPENSSH-V1 ", "A-B C-D "])
def test_a_key_block_whose_label_has_single_hyphens_is_masked(label: str) -> None:
    text = "before\n" + _block(label) + "\nafter\n"
    for stored in (redact_text(text), coding_sandbox._log_tails({"stdout": "", "stderr": text})["stderr"]):
        assert not any(line in stored for line in _BODY), label
        assert stored.startswith("before\n") and stored.endswith("after\n"), label


def test_a_hyphen_label_block_the_tail_ends_inside_is_masked() -> None:
    stored = redact_text("before\n" + "\n".join(["-----BEGIN FOO-BAR " + "PRIVATE KEY-----", *_BODY]))
    assert not any(line in stored for line in _BODY)
    assert stored.startswith("before\n")
