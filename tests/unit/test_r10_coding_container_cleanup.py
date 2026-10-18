"""Coding containers do not outlive their jobs: a failed removal is retried and logged, and every coding
container carries this worker instance's label (an id persisted on disk, so a restarted worker knows
its previous run's containers). When it starts, the worker removes only containers of its own instance
that have exited (exited or dead) longer than a grace period ago. Several stacks share this Docker
daemon: another instance's containers, running or exited, a running container of its own, and anything
matched only by name or deployment label are never touched."""

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

    def __init__(self, containers: dict[str, dict] | None = None, rm_failures: int = 0, *, ignore_status_filter: bool = False) -> None:
        self.containers = containers or {}
        self.rm_failures = rm_failures
        self.ignore_status_filter = ignore_status_filter
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[:2] == ["ps", "-aq"]:
            wanted = [part.removeprefix("label=") for part in args[2:] if part.startswith("label=")]
            statuses = [part.removeprefix("status=") for part in args[2:] if part.startswith("status=")]
            found = [
                cid
                for cid, item in self.containers.items()
                if all(label in item["labels"] for label in wanted)
                and (self.ignore_status_filter or not statuses or item["state"]["Status"] in statuses)
            ]
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


def _instance(monkeypatch: pytest.MonkeyPatch, state: Path) -> str:
    monkeypatch.delenv("AETHER_WORKER_INSTANCE_ID", raising=False)
    monkeypatch.setenv("AETHER_WORKER_STATE_DIR", str(state))
    return coding_sandbox.worker_instance_id()


def test_coding_containers_carry_this_worker_instances_label(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mine = _instance(monkeypatch, tmp_path / "state")
    args = coding_sandbox.container_arguments(tmp_path, "python:3.12-alpine", objective="make value() return 42", job_id="job-1")
    assert f"aether.worker-instance={mine}" in args


def test_the_instance_id_is_persisted_so_a_restarted_worker_keeps_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = _instance(monkeypatch, tmp_path / "state")
    assert len(first) >= 16
    assert (tmp_path / "state").is_dir() and first in "".join(path.read_text() for path in (tmp_path / "state").iterdir())
    # A new worker process reads the same file: nothing is cached in memory between the two calls.
    assert _instance(monkeypatch, tmp_path / "state") == first
    assert _instance(monkeypatch, tmp_path / "other-state") != first
    monkeypatch.setenv("AETHER_WORKER_INSTANCE_ID", "worker-7")
    assert coding_sandbox.worker_instance_id() == "worker-7"


def _container(status: str, *, started: float, finished: float | None, instance: str | None) -> dict:
    labels = ["aether.role=coding", "aether.deployment=local", "aether.coding=job"]
    if instance is not None:
        labels.append(f"aether.worker-instance={instance}")
    return {
        "labels": labels,
        "state": {
            "Status": status,
            "Created": _stamp(started + 1),
            "StartedAt": _NEVER if status == "created" else _stamp(started),
            "FinishedAt": _NEVER if finished is None else _stamp(finished),
        },
    }


def _fleet(mine: str) -> dict[str, dict]:
    grace = coding_sandbox.LEFTOVER_GRACE_SECONDS
    longest = coding_sandbox.CODING_SANDBOX_TIMEOUT_SECONDS
    return {
        "aether-code-mine-old-exited": _container("exited", started=grace * 3, finished=grace * 2, instance=mine),
        "aether-code-mine-old-dead": _container("dead", started=grace * 3, finished=grace * 2, instance=mine),
        "aether-code-mine-young-exited": _container("exited", started=grace / 2, finished=grace / 4, instance=mine),
        "aether-code-mine-running-long": _container("running", started=longest + grace * 5, finished=None, instance=mine),
        "aether-code-mine-never-started": _container("created", started=grace * 3, finished=None, instance=mine),
        "aether-code-other-running": _container("running", started=longest + grace * 5, finished=None, instance="another-instance"),
        "aether-code-other-exited": _container("exited", started=grace * 3, finished=grace * 2, instance="another-instance"),
        "aether-code-unlabelled-exited": _container("exited", started=grace * 3, finished=grace * 2, instance=None),
    }


def test_the_startup_sweep_removes_only_this_instances_exited_containers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mine = _instance(monkeypatch, tmp_path / "state")
    docker = _Docker(_fleet(mine))
    _local(monkeypatch, docker)
    removed = coding_sandbox.sweep_leftover_coding_containers(now=_NOW)
    assert sorted(removed) == ["aether-code-mine-old-dead", "aether-code-mine-old-exited"]
    assert sorted(docker.removed()) == sorted(removed)
    for kept in ("aether-code-other-running", "aether-code-other-exited", "aether-code-unlabelled-exited", "aether-code-mine-running-long", "aether-code-mine-young-exited", "aether-code-mine-never-started"):
        assert kept in docker.containers, kept
    listing = [call for call in docker.calls if call[:2] == ["ps", "-aq"]]
    assert listing and all(f"label=aether.worker-instance={mine}" in call and "label=aether.role=coding" in call for call in listing)
    assert not any(part.startswith("name=") for call in listing for part in call)


def test_the_sweep_checks_the_state_itself_not_only_dockers_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mine = _instance(monkeypatch, tmp_path / "state")
    docker = _Docker(_fleet(mine), ignore_status_filter=True)
    _local(monkeypatch, docker)
    removed = coding_sandbox.sweep_leftover_coding_containers(now=_NOW)
    assert sorted(removed) == ["aether-code-mine-old-dead", "aether-code-mine-old-exited"]
    assert "aether-code-mine-running-long" in docker.containers and "aether-code-other-running" in docker.containers


def test_a_restarted_worker_removes_its_previous_runs_exited_containers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    previous = _instance(monkeypatch, tmp_path / "state")
    args = coding_sandbox.container_arguments(tmp_path, "python:3.12-alpine", objective="make value() return 42", job_id="job-1")
    label = next(arg for arg in args if arg.startswith("aether.worker-instance="))
    grace = coding_sandbox.LEFTOVER_GRACE_SECONDS
    left = _container("exited", started=grace * 3, finished=grace * 2, instance=label.removeprefix("aether.worker-instance="))
    docker = _Docker({"aether-code-left-by-the-crash": left, "aether-code-other-exited": _container("exited", started=grace * 3, finished=grace * 2, instance="another-instance")})
    _local(monkeypatch, docker)
    # The restart: same state directory, a fresh lookup of the id.
    assert _instance(monkeypatch, tmp_path / "state") == previous
    assert coding_sandbox.sweep_leftover_coding_containers(now=_NOW) == ["aether-code-left-by-the-crash"]
    assert "aether-code-other-exited" in docker.containers


def test_the_sweep_does_nothing_when_the_worker_delegates_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _Docker({"old-exited": _container("exited", started=10_000, finished=9_000, instance=None)})
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
