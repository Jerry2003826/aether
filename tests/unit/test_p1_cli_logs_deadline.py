"""The CLI log fallback's two `docker logs` runs share one CLI_LOGS_TIMEOUT_SECONDS budget, not one each.

No real time passes: a fake clock stands in for coding_sandbox.time, a fake Popen advances it
while its stream is read, and a fake Timer records the kill interval each run was given.
"""

from __future__ import annotations

import subprocess
import types
from typing import Any

import pytest
from aether_worker import coding_sandbox


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
        if self.cost:
            self.clock.now += self.cost
            self.cost = 0.0
        payload, self.payload = self.payload, b""
        return payload

    def close(self) -> None:
        pass


class _Harness:
    def __init__(self, clock: _Clock, costs: list[float]) -> None:
        self.clock = clock
        self.costs = list(costs)
        self.started: list[str] = []
        self.intervals: list[float] = []
        self.kill_at: list[float] = []

    def popen(self, args: list[str], *, stdin: Any, stdout: Any, stderr: Any) -> Any:
        wants_stdout = stdout == subprocess.PIPE
        self.started.append("stdout" if wants_stdout else "stderr")
        stream = _Stream(self.clock, b"out\n" if wants_stdout else b"err\n", self.costs.pop(0) if self.costs else 0.0)
        return types.SimpleNamespace(
            stdout=stream if wants_stdout else None,
            stderr=None if wants_stdout else stream,
            kill=lambda: None,
            wait=lambda timeout=None: 0,
        )

    def timer(self, interval: float, function: Any) -> Any:
        self.intervals.append(interval)
        self.kill_at.append(self.clock.now + interval)
        return types.SimpleNamespace(start=lambda: None, cancel=lambda: None)


def _install(monkeypatch: pytest.MonkeyPatch, costs: list[float]) -> _Harness:
    clock = _Clock()
    harness = _Harness(clock, costs)
    monkeypatch.setattr(coding_sandbox, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(coding_sandbox, "threading", types.SimpleNamespace(Timer=harness.timer))
    monkeypatch.setattr(coding_sandbox.subprocess, "Popen", harness.popen)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    return harness


def test_the_budget_is_sixty_seconds() -> None:
    assert coding_sandbox.CLI_LOGS_TIMEOUT_SECONDS == 60


def test_a_quick_first_run_leaves_the_second_nearly_the_whole_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _install(monkeypatch, [0.0, 0.0])
    logs = coding_sandbox._cli_logs("aether-code-job1", limit=1024)
    assert logs == {"stdout": "out\n", "stderr": "err\n"}
    assert harness.started == ["stdout", "stderr"]
    assert harness.intervals == [pytest.approx(60.0), pytest.approx(60.0)]


def test_the_second_run_only_gets_what_the_first_left(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _install(monkeypatch, [45.0, 0.0])
    logs = coding_sandbox._cli_logs("aether-code-job2", limit=1024)
    assert logs == {"stdout": "out\n", "stderr": "err\n"}
    assert harness.intervals[0] == pytest.approx(60.0)
    assert harness.intervals[1] == pytest.approx(15.0)
    # Both kills land on the one shared deadline, 60s after the first run started.
    assert harness.kill_at == [pytest.approx(1060.0), pytest.approx(1060.0)]


@pytest.mark.parametrize("cost", [60.0, 75.0])
def test_no_second_run_once_the_budget_is_spent(monkeypatch: pytest.MonkeyPatch, cost: float) -> None:
    harness = _install(monkeypatch, [cost])
    logs = coding_sandbox._cli_logs("aether-code-job3", limit=1024)
    assert harness.started == ["stdout"]
    assert harness.intervals == [pytest.approx(60.0)]
    assert logs == {"stdout": "out\n", "stderr": ""}
