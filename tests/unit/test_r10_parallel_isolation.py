"""Unit tests share this machine's TMPDIR with every other pytest session running at the same time
(parallel per-commit lanes on one box). A test that keeps its scratch tree at a fixed name there
deletes, or runs on, the tree of the same test in another session. Seen on c32d367 in a control run
of 5 parallel full unit sessions: test_f26_acceptance.py::test_authorized_snapshot_fails_before_any_patch
failed in 3 of 5 sessions of the same round, its check.py gone under it.

Each test here replays that interleaving deterministically: while one session is between copying
its tree and checking it, a second session runs the same test against the same TMPDIR.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tests.unit import test_f26_acceptance as f26


def _run(test, tmp_path: Path, session: int) -> None:
    kwargs = {}
    if "tmp_path" in inspect.signature(test).parameters:
        own = tmp_path / f"session-{session}"
        own.mkdir()
        kwargs["tmp_path"] = own
    test(**kwargs)


def test_two_sessions_running_the_f26_snapshot_check_at_once_both_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setenv("TMPDIR", str(shared))
    test = f26.test_authorized_snapshot_fails_before_any_patch
    real = f26._local_check
    sessions: list[int] = []

    def check_while_another_session_runs(tree: Path):
        if not sessions:
            sessions.append(2)
            _run(test, tmp_path, 2)
        return real(tree)

    monkeypatch.setattr(f26, "_local_check", check_while_another_session_runs)
    _run(test, tmp_path, 1)
    assert sessions == [2]
