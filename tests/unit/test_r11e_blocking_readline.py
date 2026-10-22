"""A role that has bytes but never finishes a line must still hit the ready timeout.

select reports the pipe readable as soon as any byte arrives. readline then blocks until a newline.
ActiveClock only advances when the loop body returns, and a gap longer than its max step is counted
as a pause (0.1 s). A stuck readline therefore never reaches _READY_TIMEOUT. The deadline is the
product constant; this test shortens that constant so the suite does not spend a minute, and still
asserts the call fails inside whatever value the code reads.
"""

from __future__ import annotations

import os
import threading
import time

from aether_worker import role_launch


def test_a_blocking_readline_fails_within_the_ready_timeout(monkeypatch) -> None:
    monkeypatch.setattr(role_launch, "_READY_TIMEOUT", 0.4)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"partial-without-newline")
    release = threading.Event()

    class _Stdout:
        def fileno(self) -> int:
            return read_fd

        def readline(self) -> str:
            release.wait()
            return ""

    class _Proc:
        stdout = _Stdout()
        returncode = None

        def poll(self) -> None:
            return None

    outcome: list[BaseException] = []

    def _run() -> None:
        try:
            role_launch._read_ready(_Proc(), [], [])  # type: ignore[arg-type]
        except BaseException as exc:
            outcome.append(exc)

    started = time.monotonic()
    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(role_launch._READY_TIMEOUT + 0.25)
    elapsed = time.monotonic() - started
    release.set()
    os.close(read_fd)
    os.close(write_fd)
    assert not worker.is_alive(), "readline 一直阻塞时，就绪等待没有在 _READY_TIMEOUT 内结束"
    assert outcome and isinstance(outcome[0], SystemExit), outcome
    assert "没有就绪" in str(outcome[0]), outcome[0]
    assert elapsed <= role_launch._READY_TIMEOUT + 0.05, elapsed


def test_a_finished_ready_line_is_returned_without_the_full_timeout() -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"noise\naether-role-ready port=9\n")
    stdout = os.fdopen(read_fd, "r", buffering=1)

    class _Proc:
        def __init__(self) -> None:
            self.stdout = stdout
            self.returncode = None

        def poll(self) -> None:
            return None

    started = time.monotonic()
    try:
        line = role_launch._read_ready(_Proc(), [], [])  # type: ignore[arg-type]
    finally:
        os.close(write_fd)
    assert line == "aether-role-ready port=9"
    assert time.monotonic() - started < 1
