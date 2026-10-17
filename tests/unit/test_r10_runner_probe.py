"""The runner probes Docker through the SDK; the docker CLI is only needed when the SDK cannot reach a daemon."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from aether_worker import coding_sandbox, container_runtime
from aether_worker.coding_sandbox import CodingSandboxError, run_coding_sandbox
from aether_worker.runner import docker_available, probe_docker_daemon
from docker.errors import DockerException


class _Daemon:
    def info(self) -> dict[str, str]:
        return {"ServerVersion": "27.3.1"}


class _NoDaemon:
    def info(self) -> dict[str, str]:
        raise DockerException("Error while fetching server API version: socket missing")


def _setup(monkeypatch: pytest.MonkeyPatch, client: object) -> list[str]:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(container_runtime, "_CLIENT", client)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    started: list[str] = []
    monkeypatch.setattr(coding_sandbox, "_ensure_started", lambda _workspace, _image, job_id, **_kwargs: started.append(job_id))
    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", lambda job_id, **_kwargs: {"exit_code": 0, "job_id": job_id})
    return started


def test_an_sdk_reaching_the_daemon_without_a_docker_cli_enables_the_runtime_and_runs_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = _setup(monkeypatch, _Daemon())
    assert probe_docker_daemon() == {"runtime": "enabled", "server_version": "27.3.1"}
    assert docker_available() is True
    sandbox = run_coding_sandbox(tmp_path, objective="修复 add", job_id="job-sdk-only")
    assert started == ["job-sdk-only"]
    assert sandbox["exit_code"] == 0


def test_neither_sdk_daemon_nor_docker_cli_is_cli_missing_and_sandbox_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = _setup(monkeypatch, _NoDaemon())
    assert probe_docker_daemon() == {"runtime": "unsupported", "reason": "cli_missing"}
    assert docker_available() is False
    with pytest.raises(CodingSandboxError) as raised:
        run_coding_sandbox(tmp_path, objective="修复 add", job_id="job-none")
    assert raised.value.code == "sandbox_unavailable"
    assert started == []


def test_an_unreachable_sdk_with_a_docker_cli_is_daemon_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch, _NoDaemon())
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    assert probe_docker_daemon() == {"runtime": "unsupported", "reason": "daemon_unreachable"}


def test_after_an_enabled_probe_starting_a_job_does_not_require_the_docker_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = _setup(monkeypatch, _Daemon())
    monkeypatch.setattr("aether_worker.runner.probe_docker_daemon", lambda: {"runtime": "enabled", "server_version": "27.3.1"})
    run_coding_sandbox(tmp_path, objective="修复 add", job_id="job-after-probe")
    assert started == ["job-after-probe"]
