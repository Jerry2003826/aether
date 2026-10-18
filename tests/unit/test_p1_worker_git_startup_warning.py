from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

import pytest
from aether_worker import container_runtime, service
from aether_worker.goal_effect import WorkerEndpoints


class _Stop(Exception):
    pass


def _endpoints() -> WorkerEndpoints:
    return WorkerEndpoints(
        worker_url="",
        verifier_url="",
        dispatcher_url="",
        artifact_root=Path("/nonexistent"),
        temporal_address="127.0.0.1:1",
        task_queue="aether-test",
    )


_GIT_MISSING = "worker_git_missing"


def _serve_until_connect(monkeypatch: pytest.MonkeyPatch, path: str) -> list[str]:
    order: list[str] = []
    monkeypatch.setenv("PATH", path)

    # The worker's start opens no Docker client (the runner sweeps leftover containers); a real
    # daemon or the host's Docker credentials must never reach this test.
    def no_docker() -> object:
        order.append("docker_client")
        raise AssertionError("serve() opened a docker client")

    monkeypatch.setattr(container_runtime, "docker_client", no_docker)
    monkeypatch.setattr(service, "configure", lambda endpoints: order.append("configure"))

    async def connect(*args: object, **kwargs: object) -> object:
        order.append("connect")
        raise _Stop

    monkeypatch.setattr(service.Client, "connect", connect)
    monkeypatch.setattr(service, "stop_children", lambda children: None)
    monkeypatch.setattr(service, "stop_recorded_roles", lambda: None)
    with pytest.raises(_Stop):
        asyncio.run(service.serve(_endpoints()))
    return order


def _git_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The worker's own git-missing records. Other loggers may mention "git" (docker.auth logs a
    registry user such as githubactions at DEBUG); they are not this warning."""
    return [record for record in caplog.records if record.name == "aether.worker" and _GIT_MISSING in record.getMessage()]


def test_serve_warns_once_when_git_is_not_on_path_and_keeps_starting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()
    caplog.set_level(logging.DEBUG)
    order = _serve_until_connect(monkeypatch, str(empty))
    warnings = _git_warnings(caplog)
    assert len(warnings) == 1
    record = warnings[0]
    assert record.levelno == logging.WARNING
    assert record.name == "aether.worker"
    assert record.getMessage() == (
        "worker_git_missing git is not on PATH; coding job patches will be patch_unavailable"
    )
    assert order == ["configure", "connect"]


def test_serve_does_not_warn_when_git_is_on_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    git = shutil.which("git")
    assert git is not None
    caplog.set_level(logging.DEBUG)
    order = _serve_until_connect(monkeypatch, os.path.dirname(git))
    assert _git_warnings(caplog) == []
    assert order == ["configure", "connect"]
