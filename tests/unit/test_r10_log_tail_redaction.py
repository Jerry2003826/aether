"""Stored sandbox log tails are redacted over the whole text, not line by line: a key block or a
connection string that spans lines is masked, and a secret the tail cut in half at its start is not
left behind as a fragment."""

from __future__ import annotations

from aether_worker import coding_sandbox

# Readable fixture phrases, not credentials. The armor lines are assembled at run time.
_FIXTURE_PASSWORD = "this-is-a-fixture-password"
_KEY_LINES = ["fixture key body line one", "fixture key body line two", "fixture key body line three"]
_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_END = "-----END " + "PRIVATE KEY-----"


def _stderr(text: str) -> str:
    return coding_sandbox._log_tails({"stdout": "", "stderr": text})["stderr"]


def test_a_key_block_spanning_lines_is_masked() -> None:
    text = "before\n" + "\n".join([_BEGIN, *_KEY_LINES, _END]) + "\nafter\n"
    stored = _stderr(text)
    assert not any(line in stored for line in _KEY_LINES)
    assert stored.startswith("before\n") and stored.endswith("after\n")


def test_a_key_block_the_tail_ends_inside_is_masked() -> None:
    stored = _stderr("before\n" + "\n".join([_BEGIN, *_KEY_LINES]))
    assert not any(line in stored for line in _KEY_LINES)
    assert stored.startswith("before\n")


def _cut_at(before: str, after: str) -> tuple[str, str]:
    """A log whose stored tail starts exactly at the boundary between `before` and `after`."""
    pad = ("later log line\n" * (coding_sandbox.LOG_TAIL_LIMIT // 15 + 2))[: coding_sandbox.LOG_TAIL_LIMIT - len(after)]
    assert len(after + pad) == coding_sandbox.LOG_TAIL_LIMIT
    return "earlier log line\n" * 10 + before + after + pad, pad


def test_a_key_block_the_tail_starts_inside_is_masked() -> None:
    first = _KEY_LINES[0]
    before = _BEGIN + "\n" + first[:10]
    after = first[10:] + "\n" + "\n".join(_KEY_LINES[1:]) + "\n" + _END + "\n"
    text, pad = _cut_at(before, after)
    stored = _stderr(text)
    assert first[10:] not in stored and not any(line in stored for line in _KEY_LINES[1:])
    assert stored.endswith(pad[-60:])


def test_a_connection_string_wrapped_across_lines_is_masked() -> None:
    half = len(_FIXTURE_PASSWORD) // 2
    text = f"before\npostgresql://aether:{_FIXTURE_PASSWORD[:half]}\n{_FIXTURE_PASSWORD[half:]}@db:5432/aether\nafter\n"
    stored = _stderr(text)
    assert _FIXTURE_PASSWORD[:half] not in stored and _FIXTURE_PASSWORD[half:] not in stored
    assert stored.startswith("before\n") and stored.endswith("after\n")


def test_a_secret_cut_in_half_at_the_start_of_the_tail_leaves_no_fragment() -> None:
    half = len(_FIXTURE_PASSWORD) // 2
    before = f"postgresql://aether:{_FIXTURE_PASSWORD[:half]}"
    after = f"{_FIXTURE_PASSWORD[half:]}@db:5432/aether\nnext line\n"
    text, pad = _cut_at(before, after)
    stored = _stderr(text)
    assert _FIXTURE_PASSWORD[half:] not in stored
    assert "next line\n" in stored and stored.endswith(pad[-60:])


def test_tails_that_were_not_cut_keep_their_first_line() -> None:
    assert _stderr("first line\nsecond line\n") == "first line\nsecond line\n"
