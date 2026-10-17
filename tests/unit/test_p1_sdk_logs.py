"""Coding sandbox logs come from the Docker SDK; the CLI is only a fallback when the SDK is unavailable."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from aether_worker import coding_sandbox, container_runtime
from docker.errors import DockerException


class _Container:
    def logs(self, *, stdout: bool, stderr: bool, stream: bool, follow: bool) -> Any:
        assert follow is False
        if stdout:
            return iter([b"working\n", b'RESULT {"passed": 1}\n'])
        return iter([b"warn: slow\n"])


class _Containers:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get(self, name: str) -> _Container:
        self.asked.append(name)
        return _Container()


class _Client:
    def __init__(self) -> None:
        self.containers = _Containers()


def _local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)


def _no_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> Any:
        raise DockerException("no docker daemon reachable through the SDK")

    monkeypatch.setattr(container_runtime, "docker_client", unavailable)


def _fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "docker"
    script.write_text('#!/bin/sh\n[ "$1" = logs ] || exit 2\necho "cli out $2"\necho "cli err $2" >&2\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_sdk_logs_are_returned_without_a_docker_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    _local(monkeypatch)
    client = _Client()
    monkeypatch.setattr(container_runtime, "docker_client", lambda: client)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: None)
    logs = coding_sandbox._bounded_logs("aether-coding-job-1", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert client.containers.asked == ["aether-coding-job-1"]
    assert logs == {"stdout": 'working\nRESULT {"passed": 1}\n', "stderr": "warn: slow\n"}


def test_sdk_logs_win_over_the_cli_when_both_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _local(monkeypatch)
    client = _Client()
    monkeypatch.setattr(container_runtime, "docker_client", lambda: client)
    monkeypatch.setenv("PATH", f"{_fake_cli(tmp_path).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    logs = coding_sandbox._bounded_logs("aether-coding-job-2", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert logs["stdout"].startswith("working")
    assert "cli" not in logs["stdout"] + logs["stderr"]


def test_the_cli_is_the_fallback_when_the_sdk_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _local(monkeypatch)
    _no_sdk(monkeypatch)
    monkeypatch.setenv("PATH", f"{_fake_cli(tmp_path).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    logs = coding_sandbox._bounded_logs("aether-coding-job-3", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert logs == {"stdout": "cli out aether-coding-job-3\n", "stderr": "cli err aether-coding-job-3\n"}


def test_cli_fallback_logs_keep_only_the_bounded_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _local(monkeypatch)
    _no_sdk(monkeypatch)
    monkeypatch.setenv("PATH", f"{_fake_cli(tmp_path).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    logs = coding_sandbox._bounded_logs("aether-coding-job-4", limit=6)
    assert logs["stdout"] == "job-4\n"


def test_no_sdk_and_no_cli_yields_empty_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    _local(monkeypatch)
    _no_sdk(monkeypatch)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: None)
    assert coding_sandbox._bounded_logs("aether-coding-job-5", limit=1024) == {"stdout": "", "stderr": ""}
