"""The console browser test waits on the Next.js dev server by its own output (r11c, triage category B):
ready at its "Ready in" line, even past the old fixed 90 s while it keeps printing; failed at once on
its error marker or when it exits; failed when it stops printing for the stall limit or overruns the
total. Fake clock: nothing here sleeps."""

from __future__ import annotations

import pytest

from tests.integration.startup_wait import (
    WEB_STALL_LIMIT,
    WEB_TOTAL_BUDGET,
    wait_web_ready,
)


class _Script:
    def __init__(self, lines: list[tuple[float, str]], exit_at: float | None = None) -> None:
        self.now = 0.0
        self.lines = sorted(lines)
        self.bucket: list[str] = []
        self.exit_at = exit_at

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        while self.lines and self.lines[0][0] <= self.now:
            self.bucket.append(self.lines.pop(0)[1] + "\n")

    def poll(self) -> int | None:
        return 1 if self.exit_at is not None and self.now >= self.exit_at else None

    def wait(self) -> None:
        wait_web_ready(self.bucket, self, clock=self.clock, sleep=self.sleep)  # type: ignore[arg-type]


def test_a_slow_next_start_that_keeps_printing_is_waited_for() -> None:
    script = _Script([(0.0, "   ▲ Next.js 15"), (40.0, " ✓ Starting..."), (80.0, " ○ Compiling instrumentation ..."), (110.0, " ✓ Ready in 110s")])
    script.wait()
    assert 110.0 <= script.now < 111.0


def test_a_next_error_fails_at_once() -> None:
    script = _Script([(0.0, " ✓ Starting..."), (3.0, " ⨯ Failed to start server")])
    with pytest.raises(AssertionError, match="start-failed"):
        script.wait()
    assert script.now < 4.0


def test_a_silent_or_endless_next_start_fails_within_its_limits() -> None:
    silent = _Script([(0.0, " ✓ Starting...")])
    with pytest.raises(AssertionError, match="stall"):
        silent.wait()
    assert silent.now < WEB_STALL_LIMIT + 2
    chatty = _Script([(10.0 * index, f" ○ step {index}") for index in range(100)])
    with pytest.raises(AssertionError, match="total"):
        chatty.wait()
    assert chatty.now < WEB_TOTAL_BUDGET + 2


def test_an_exited_next_fails_at_once() -> None:
    script = _Script([(0.0, " ✓ Starting...")], exit_at=2.0)
    with pytest.raises(AssertionError, match="退出"):
        script.wait()
