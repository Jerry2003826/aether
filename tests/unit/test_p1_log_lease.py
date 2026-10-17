"""The exit-log read keeps the coding lease alive in slices, on a fake clock, without changing its deadline."""

from __future__ import annotations

import io
import itertools
from types import SimpleNamespace
from typing import Any

import pytest
from aether_worker import coding_jobs, coding_sandbox
from docker.errors import DockerException


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _Process:
    """`docker logs` that never ends on its own: it hangs until killed, then its pipe yields what it wrote."""

    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.killed = False
        pipe = io.BytesIO(b"x" * 100 + b'\n{"passed": 1}\n')
        self.stdout = pipe if kwargs.get("stdout") is not None and kwargs["stdout"] != -3 else None
        self.stderr = io.BytesIO(b"err") if self.stdout is None else None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return -9


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = _Clock()
        self.processes: list[_Process] = []
        world = self

        class _Thread:
            # The reader is blocked on a pipe that only closes when its process is killed.
            def __init__(self, *, target: Any, daemon: bool = False, **_k: Any) -> None:
                self.target = target
                self.done = False

            def start(self) -> None:
                pass

            def _finish(self) -> None:
                if not self.done:
                    self.done = True
                    self.target()

            def join(self, timeout: float | None = None) -> None:
                if world.processes and world.processes[-1].killed:
                    self._finish()
                    return
                assert timeout is not None, "an unbounded join would really wait"
                world.clock.now += timeout

            def is_alive(self) -> bool:
                return not self.done

        def popen(args: list[str], **kwargs: Any) -> _Process:
            process = _Process(args, **kwargs)
            world.processes.append(process)
            return process

        def no_logs(*_a: Any, **_k: Any) -> Any:
            raise DockerException("daemon unreachable")

        monkeypatch.setattr(coding_sandbox, "time", SimpleNamespace(monotonic=self.clock.monotonic, sleep=lambda _s: None))
        monkeypatch.setattr(coding_sandbox, "threading", SimpleNamespace(Thread=_Thread, Timer=_never_fires))
        monkeypatch.setattr(coding_sandbox.subprocess, "Popen", popen)
        monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
        monkeypatch.setattr(coding_sandbox, "container_logs", no_logs)
        monkeypatch.setattr(coding_sandbox, "_exit_code", lambda _name: 0)
        monkeypatch.setattr(coding_sandbox, "_sandbox_docker", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=""))


class _never_fires:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        pass


def test_the_cli_log_fallback_beats_the_heartbeat_for_its_whole_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    beats: list[float] = []
    result = coding_sandbox._collect_exit("aether-code-x", heartbeat=lambda: beats.append(world.clock.now))
    deadline = coding_sandbox.CLI_LOGS_TIMEOUT_SECONDS
    # The shared deadline is unchanged: the stdout read used all of it and was then killed.
    assert world.clock.now == pytest.approx(deadline)
    assert world.processes[0].killed
    assert result["result"] == {"passed": 1}
    assert len(beats) >= 2, beats
    marks = [0.0, *beats, float(deadline)]
    gaps = [later - earlier for earlier, later in itertools.pairwise(marks)]
    assert max(gaps) < coding_jobs.LEASE_SECONDS, gaps
    assert max(gaps) <= coding_jobs.LEASE_SECONDS / 3 + 1e-9, gaps


def test_a_failing_heartbeat_ends_the_log_read_and_kills_docker_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)

    class _Gone(Exception):
        pass

    def heartbeat() -> None:
        raise _Gone

    with pytest.raises(_Gone):
        coding_sandbox._collect_exit("aether-code-x", heartbeat=heartbeat)
    assert world.clock.now < coding_sandbox.CLI_LOGS_TIMEOUT_SECONDS
    assert world.processes[0].killed
