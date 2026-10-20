"""A Docker SDK that connects but then fails on the container falls back to the CLI, not to empty logs.

docker.from_env() can succeed while the first containers.get() (or logs()) raises a DockerException,
for example a 500 from the daemon. That is the same "SDK cannot serve the logs" case as an
unreachable daemon, so the CLI fallback, bounded by its one shared deadline, must run. A container
the daemon says does not exist (NotFound) still has no logs. No real time passes: the CLI runs are
a fake Popen under a fake clock and a fake Timer, as in test_p1_cli_logs_deadline.
"""

from __future__ import annotations

import subprocess
import types
from typing import Any

import pytest
from aether_worker import coding_sandbox, container_runtime
from docker.errors import APIError, DockerException, NotFound


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Stream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _size: int = -1) -> bytes:
        payload, self.payload = self.payload, b""
        return payload

    def close(self) -> None:
        pass


class _Cli:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.started: list[list[str]] = []
        self.intervals: list[float] = []

    def popen(self, args: list[str], *, stdin: Any, stdout: Any, stderr: Any) -> Any:
        self.started.append(list(args))
        wants_stdout = stdout == subprocess.PIPE
        stream = _Stream(f"cli out {args[-1]}\n".encode() if wants_stdout else f"cli err {args[-1]}\n".encode())
        return types.SimpleNamespace(
            stdout=stream if wants_stdout else None,
            stderr=None if wants_stdout else stream,
            kill=lambda: None,
            wait=lambda timeout=None: 0,
        )

    def timer(self, interval: float, function: Any) -> Any:
        self.intervals.append(interval)
        return types.SimpleNamespace(start=lambda: None, cancel=lambda: None)


class _BrokenLogsContainer:
    def logs(self, **_kwargs: Any) -> Any:
        raise APIError("500 Server Error: logs unavailable")


class _Containers:
    def __init__(self, failure: Exception | None) -> None:
        self.failure = failure
        self.asked: list[str] = []

    def get(self, name: str) -> Any:
        self.asked.append(name)
        if self.failure is not None:
            raise self.failure
        return _BrokenLogsContainer()


class _Client:
    def __init__(self, failure: Exception | None) -> None:
        self.containers = _Containers(failure)


def _install(monkeypatch: pytest.MonkeyPatch, client: _Client) -> _Cli:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(container_runtime, "docker_client", lambda: client)
    clock = _Clock()
    cli = _Cli(clock)
    monkeypatch.setattr(coding_sandbox, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(coding_sandbox, "threading", types.SimpleNamespace(Timer=cli.timer))
    monkeypatch.setattr(coding_sandbox.subprocess, "Popen", cli.popen)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    return cli


@pytest.mark.parametrize(
    "failure",
    [APIError("500 Server Error: Internal Server Error"), DockerException("daemon went away")],
    ids=["api_error", "docker_exception"],
)
def test_a_failing_containers_get_falls_back_to_the_cli(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    client = _Client(failure)
    cli = _install(monkeypatch, client)
    logs = coding_sandbox._bounded_logs("aether-code-job7", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert client.containers.asked == ["aether-code-job7"]
    assert logs == {"stdout": "cli out aether-code-job7\n", "stderr": "cli err aether-code-job7\n"}
    assert [args[1:] for args in cli.started] == [["logs", "aether-code-job7"], ["logs", "aether-code-job7"]]
    # Still the one shared CLI deadline: two runs, each killed no later than 60s after the first began.
    assert cli.intervals == [pytest.approx(coding_sandbox.CLI_LOGS_TIMEOUT_SECONDS)] * 2


def test_a_failing_logs_call_falls_back_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(None)
    cli = _install(monkeypatch, client)
    logs = coding_sandbox._bounded_logs("aether-code-job8", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert logs == {"stdout": "cli out aether-code-job8\n", "stderr": "cli err aether-code-job8\n"}
    assert len(cli.started) == 2


def test_a_container_the_daemon_does_not_know_has_no_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(NotFound("No such container: aether-code-job9"))
    cli = _install(monkeypatch, client)
    logs = coding_sandbox._bounded_logs("aether-code-job9", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert logs == {"stdout": "", "stderr": ""}
    assert cli.started == []


@pytest.mark.parametrize(
    "failure",
    [APIError("500 Server Error: Internal Server Error"), DockerException("daemon went away")],
    ids=["api_error", "docker_exception"],
)
def test_container_logs_does_not_turn_an_sdk_failure_into_empty_logs(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    monkeypatch.setattr(container_runtime, "docker_client", lambda: _Client(failure))
    with pytest.raises(DockerException):
        container_runtime.container_logs("aether-code-job10")
