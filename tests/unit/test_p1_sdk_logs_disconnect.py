"""A daemon connection that drops while the SDK reads logs falls back to the CLI, not to a failed job.

requests raises ConnectionError (or another RequestException) when the daemon socket goes away
mid-stream; that is not a DockerException, but it is the same "the SDK cannot serve the logs"
case. The CLI fallback still runs under its one shared CLI_LOGS_TIMEOUT_SECONDS deadline. No real
time passes: the CLI runs are a fake Popen under a fake clock and a fake Timer.
"""

from __future__ import annotations

import subprocess
import types
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from aether_worker import coding_sandbox, container_runtime


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Stream:
    def __init__(self, clock: _Clock, payload: bytes, cost: float) -> None:
        self.clock = clock
        self.payload = payload
        self.cost = cost

    def read(self, _size: int = -1) -> bytes:
        self.clock.now += self.cost
        self.cost = 0.0
        payload, self.payload = self.payload, b""
        return payload

    def close(self) -> None:
        pass


class _Cli:
    def __init__(self, clock: _Clock, costs: list[float]) -> None:
        self.clock = clock
        self.costs = list(costs)
        self.started: list[list[str]] = []
        self.intervals: list[float] = []

    def popen(self, args: list[str], *, stdin: Any, stdout: Any, stderr: Any) -> Any:
        self.started.append(list(args))
        wants_stdout = stdout == subprocess.PIPE
        payload = f"cli out {args[-1]}\n".encode() if wants_stdout else f"cli err {args[-1]}\n".encode()
        stream = _Stream(self.clock, payload, self.costs.pop(0) if self.costs else 0.0)
        return types.SimpleNamespace(
            stdout=stream if wants_stdout else None,
            stderr=None if wants_stdout else stream,
            kill=lambda: None,
            wait=lambda timeout=None: 0,
        )

    def timer(self, interval: float, function: Any) -> Any:
        self.intervals.append(interval)
        return types.SimpleNamespace(start=lambda: None, cancel=lambda: None)


def _dropping(failure: Exception) -> Iterator[bytes]:
    yield b"partial line before the daemon went away\n"
    raise failure


class _DroppingContainer:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def logs(self, **_kwargs: Any) -> Any:
        return _dropping(self.failure)


class _Containers:
    def __init__(self, *, on_get: Exception | None, mid_stream: Exception | None) -> None:
        self.on_get = on_get
        self.mid_stream = mid_stream

    def get(self, _name: str) -> Any:
        if self.on_get is not None:
            raise self.on_get
        assert self.mid_stream is not None
        return _DroppingContainer(self.mid_stream)


def _install(monkeypatch: pytest.MonkeyPatch, containers: _Containers, costs: list[float] | None = None) -> _Cli:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(container_runtime, "docker_client", lambda: types.SimpleNamespace(containers=containers))
    clock = _Clock()
    cli = _Cli(clock, costs or [])
    monkeypatch.setattr(coding_sandbox, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(coding_sandbox, "threading", types.SimpleNamespace(Timer=cli.timer))
    monkeypatch.setattr(coding_sandbox.subprocess, "Popen", cli.popen)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    return cli


_FAILURES = [
    requests.exceptions.ConnectionError("Connection aborted: daemon closed the socket"),
    requests.exceptions.ChunkedEncodingError("Connection broken: incomplete chunk"),
    requests.exceptions.ReadTimeout("Read timed out"),
]
_IDS = ["connection_error", "chunked_encoding_error", "read_timeout"]


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_a_connection_dropped_mid_stream_falls_back_to_the_cli(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    cli = _install(monkeypatch, _Containers(on_get=None, mid_stream=failure))
    logs = coding_sandbox._bounded_logs("aether-code-job11", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert logs == {"stdout": "cli out aether-code-job11\n", "stderr": "cli err aether-code-job11\n"}
    assert [args[1:] for args in cli.started] == [["logs", "aether-code-job11"], ["logs", "aether-code-job11"]]
    assert cli.intervals == [pytest.approx(coding_sandbox.CLI_LOGS_TIMEOUT_SECONDS)] * 2


@pytest.mark.parametrize("failure", _FAILURES, ids=_IDS)
def test_a_connection_dropped_on_containers_get_falls_back_to_the_cli(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    cli = _install(monkeypatch, _Containers(on_get=failure, mid_stream=None))
    logs = coding_sandbox._bounded_logs("aether-code-job12", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert logs == {"stdout": "cli out aether-code-job12\n", "stderr": "cli err aether-code-job12\n"}
    assert len(cli.started) == 2


def test_the_disconnect_fallback_keeps_the_one_shared_cli_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stdout run uses 45 of the 60 seconds; the stderr run gets only the 15 left, not a fresh 60.
    failure = requests.exceptions.ConnectionError("Connection aborted")
    cli = _install(monkeypatch, _Containers(on_get=None, mid_stream=failure), costs=[45.0])
    coding_sandbox._bounded_logs("aether-code-job13", limit=coding_sandbox.LOG_CAPTURE_LIMIT)
    assert cli.intervals == [pytest.approx(coding_sandbox.CLI_LOGS_TIMEOUT_SECONDS), pytest.approx(coding_sandbox.CLI_LOGS_TIMEOUT_SECONDS - 45.0)]
