"""Coding containers do not outlive their jobs: a failed removal is retried and logged, every coding
container carries this deployment's label, and the worker sweeps leftover coding containers of its
own deployment when it starts (stopped ones past a grace period, running ones past the longest run
a coding sandbox may take). Other deployments' containers and young ones are never touched."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aether_worker import coding_sandbox, service

_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_NEVER = "0001-01-01T00:00:00Z"


def _stamp(seconds_ago: float) -> str:
    return (_NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


class _Docker:
    """A fake docker CLI: containers by id with status and times; records every command."""

    def __init__(self, containers: dict[str, dict[str, str]] | None = None, rm_failures: int = 0) -> None:
        self.containers = containers or {}
        self.rm_failures = rm_failures
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[:2] == ["ps", "-aq"]:
            wanted = [part.removeprefix("label=") for part in args[2:] if part.startswith("label=")]
            found = [cid for cid, item in self.containers.items() if all(label in item["labels"] for label in wanted)]
            return subprocess.CompletedProcess(args, 0, "".join(f"{cid}\n" for cid in found), "")
        if args[:2] == ["inspect", "-f"]:
            if args[2] == "{{.State.ExitCode}}":
                return subprocess.CompletedProcess(args, 0, "0\n", "")
            item = self.containers.get(args[3])
            if item is None:
                return subprocess.CompletedProcess(args, 1, "", "no such container")
            state = {key: value for key, value in item["state"].items() if key != "Created"}
            return subprocess.CompletedProcess(args, 0, json.dumps({"State": state, "Created": item["state"]["Created"]}) + "\n", "")
        if args[:2] == ["rm", "-f"]:
            if self.rm_failures:
                self.rm_failures -= 1
                return subprocess.CompletedProcess(args, 1, "", "removal in progress")
            self.containers.pop(args[2], None)
            return subprocess.CompletedProcess(args, 0, args[2] + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def removed(self) -> list[str]:
        return [call[2] for call in self.calls if call[:2] == ["rm", "-f"]]


def _local(monkeypatch: pytest.MonkeyPatch, docker: _Docker) -> None:
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", docker)
    monkeypatch.setattr(coding_sandbox, "_docker", docker)


def test_a_failed_removal_after_exit_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _Docker(rm_failures=1)
    _local(monkeypatch, docker)
    monkeypatch.setattr(coding_sandbox, "container_logs", lambda _name, *, limit: (b'RESULT {"passed": 1, "failed": 0, "cases": []}\n', b""))
    coding_sandbox._collect_exit("aether-code-left1")
    assert docker.removed() == ["aether-code-left1", "aether-code-left1"]


def test_a_removal_that_keeps_failing_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    docker = _Docker(rm_failures=5)
    _local(monkeypatch, docker)
    monkeypatch.setattr(coding_sandbox, "container_logs", lambda _name, *, limit: (b'RESULT {"passed": 1, "failed": 0, "cases": []}\n', b""))
    with caplog.at_level(logging.WARNING, logger="aether.worker.coding"):
        collected = coding_sandbox._collect_exit("aether-code-left2")
    assert collected["exit_code"] == 0
    assert any("coding_container_left" in record.getMessage() and "aether-code-left2" in record.getMessage() for record in caplog.records)


def test_coding_containers_carry_the_deployment_label(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AETHER_DEPLOYMENT_ID", raising=False)
    args = coding_sandbox.container_arguments(tmp_path, "python:3.12-alpine", objective="make value() return 42", job_id="job-1")
    assert "aether.deployment=local" in args
    monkeypatch.setenv("AETHER_DEPLOYMENT_ID", "staging-2")
    args = coding_sandbox.container_arguments(tmp_path, "python:3.12-alpine", objective="make value() return 42", job_id="job-1")
    assert "aether.deployment=staging-2" in args


def _container(status: str, *, started: float, finished: float | None, deployment: str = "local") -> dict:
    return {
        "labels": ["aether.role=coding", f"aether.deployment={deployment}", "aether.coding=job"],
        "state": {
            "Status": status,
            "Created": _stamp(started + 1),
            "StartedAt": _NEVER if status == "created" else _stamp(started),
            "FinishedAt": _NEVER if finished is None else _stamp(finished),
        },
    }


def test_the_startup_sweep_removes_only_this_deployments_leftovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_DEPLOYMENT_ID", raising=False)
    grace = coding_sandbox.LEFTOVER_GRACE_SECONDS
    longest = coding_sandbox.CODING_SANDBOX_TIMEOUT_SECONDS
    docker = _Docker(
        {
            "old-exited": _container("exited", started=grace * 3, finished=grace * 2),
            "young-exited": _container("exited", started=grace / 2, finished=grace / 4),
            "old-dead": _container("dead", started=grace * 3, finished=grace * 2),
            "never-started": _container("created", started=grace * 3, finished=None),
            "running-past-the-longest-run": _container("running", started=longest + grace * 2, finished=None),
            "running-young": _container("running", started=longest / 2, finished=None),
            "other-deployment": _container("exited", started=grace * 3, finished=grace * 2, deployment="other"),
        }
    )
    _local(monkeypatch, docker)
    removed = coding_sandbox.sweep_leftover_coding_containers(now=_NOW)
    assert sorted(removed) == ["never-started", "old-dead", "old-exited", "running-past-the-longest-run"]
    assert sorted(docker.removed()) == sorted(removed)
    assert "other-deployment" in docker.containers and "young-exited" in docker.containers and "running-young" in docker.containers
    listing = [call for call in docker.calls if call[:2] == ["ps", "-aq"]]
    assert listing and all("label=aether.role=coding" in call and "label=aether.deployment=local" in call for call in listing)


def test_the_sweep_does_nothing_when_the_worker_delegates_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _Docker({"old-exited": _container("exited", started=10_000, finished=9_000)})
    _local(monkeypatch, docker)
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: True)
    assert coding_sandbox.sweep_leftover_coding_containers(now=_NOW) == []
    assert docker.calls == []


def test_a_failing_sweep_does_not_stop_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        raise coding_sandbox.CodingSandboxError("sandbox_unavailable")

    _local(monkeypatch, _Docker())
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", broken)
    assert coding_sandbox.sweep_leftover_coding_containers(now=_NOW) == []


def test_the_worker_sweeps_when_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    swept: list[bool] = []

    class Stop(Exception):
        pass

    def endpoints() -> None:
        raise Stop

    monkeypatch.setattr(service, "warn_if_git_missing", lambda: None)
    monkeypatch.setattr(service, "sweep_leftover_coding_containers", lambda: swept.append(True) or [], raising=False)
    monkeypatch.setattr(service, "_endpoints_for_process", endpoints)
    with pytest.raises(Stop):
        asyncio.run(service.serve())
    assert swept == [True]
