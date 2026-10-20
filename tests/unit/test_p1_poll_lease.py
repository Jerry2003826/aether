"""An exception from the poll callback (a lost lease) stops the coding container before it propagates."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aether_worker import coding_sandbox

_JOB = "00000000-0000-0000-0000-0000000000cc"


class _Gone(Exception):
    pass


def test_a_raising_poll_callback_removes_the_running_container(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "_container_status", lambda _name: "running")
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: ["cid-1"])
    monkeypatch.setattr(coding_sandbox, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _s: None))

    def docker(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(list(args))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", docker)
    polls: list[int] = []

    def on_poll() -> None:
        polls.append(1)
        if len(polls) == 2:
            raise _Gone

    with pytest.raises(_Gone):
        coding_sandbox._wait_until_stopped(
            _JOB,
            timeout_seconds=1000,
            stop_requested=None,
            deadline_reached=None,
            on_poll=on_poll,
            grace_seconds=1,
            poll_seconds=0,
        )
    assert ["rm", "-f", coding_sandbox.container_name(_JOB)] in commands
    assert ["rm", "-f", "cid-1"] in commands


def test_the_container_removal_is_best_effort_and_keeps_the_callbacks_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "_container_status", lambda _name: "running")
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: ["cid-1"])
    tried: list[list[str]] = []

    def broken(args: list[str], **_kwargs: Any) -> Any:
        tried.append(list(args))
        raise OSError("docker went away")

    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", broken)

    def on_poll() -> None:
        raise _Gone

    with pytest.raises(_Gone):
        coding_sandbox._wait_until_stopped(
            _JOB,
            timeout_seconds=1000,
            stop_requested=None,
            deadline_reached=None,
            on_poll=on_poll,
            grace_seconds=1,
            poll_seconds=0,
        )
    assert tried, "the container removal must at least be attempted"
