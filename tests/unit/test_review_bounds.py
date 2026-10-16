"""Review bounds: log tails stay short, and an evaluation deadline is a real cutoff."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from aether_worker.coding_sandbox import read_bounded_tail
from aether_worker.skill_eval import SkillEvalError
from aether_worker.skill_execute import evaluation_deadline
from aether_worker.verification import VerificationError, run_grader_container


def test_log_tail_keeps_only_the_requested_bytes() -> None:
    payload = b"A" * 1000 + b"RESULT tail"
    kept = read_bounded_tail(io.BytesIO(payload), 16)
    assert kept.encode("utf-8") == payload[-16:]
    assert kept.endswith("RESULT tail")


def test_zero_active_seconds_is_already_due() -> None:
    deadline = evaluation_deadline({"active_seconds": 0}, now=10.0)
    assert deadline == 10.0


def test_missing_active_seconds_sets_no_deadline() -> None:
    assert evaluation_deadline({"max_tokens": 10}, now=10.0) is None


def test_grader_timeout_removes_the_named_container(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        seen.append(args)
        if args[0] == "run":
            raise subprocess.TimeoutExpired(cmd=["docker", *args], timeout=0 if timeout is None else timeout)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("aether_worker.verification._docker", fake)
    with pytest.raises(VerificationError) as rejected:
        run_grader_container(Path("/tmp/unused"), timeout_seconds=0.5)
    assert rejected.value.code == "deadline_exceeded"
    assert seen[0][0] == "run"
    assert "--name" in seen[0]
    assert any(command[0] == "rm" and "-f" in command for command in seen[1:])


def test_negative_active_seconds_is_a_deadline_error() -> None:
    with pytest.raises(SkillEvalError) as rejected:
        evaluation_deadline({"active_seconds": -1}, now=1.0)
    assert rejected.value.code == "deadline_exceeded"
