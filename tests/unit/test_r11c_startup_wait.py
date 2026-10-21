"""The test-side wait on a starting worker (r11c, triage category A). It returns at aether-worker-ready,
fails at once on aether-worker-start-failed or process exit, fails when no progress line arrives for
the stall limit, and fails when the whole start (image preparation excluded; the image is prebuilt)
overruns the product's startup budget. Driven by a fake clock: nothing here sleeps."""

from __future__ import annotations

import pytest
from aether_worker import startup_budget

from tests.integration.startup_wait import wait_worker_ready


class _Script:
    """A fake clock and process: lines appear in bucket at their scheduled times."""

    def __init__(self, lines: list[tuple[float, str]], exit_at: float | None = None) -> None:
        self.now = 0.0
        self.lines = sorted(lines)
        self.bucket: list[str] = []
        self.exit_at = exit_at
        self.returncode: int | None = None

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        while self.lines and self.lines[0][0] <= self.now:
            self.bucket.append(self.lines.pop(0)[1] + "\n")

    def poll(self) -> int | None:
        if self.exit_at is not None and self.now >= self.exit_at:
            self.returncode = 1
        return self.returncode

    def wait(self, **budget: float) -> None:
        wait_worker_ready(self.bucket, self, clock=self.clock, sleep=self.sleep, **budget)  # type: ignore[arg-type]


def _phase(name: str) -> str:
    return f"aether-worker-phase name={name} elapsed=0"


def test_a_slow_start_that_keeps_moving_is_waited_for_past_the_old_90_seconds() -> None:
    steps = [(50.0 * index, _phase(f"step-{index}")) for index in range(5)]
    script = _Script([*steps, (230.0, "aether-worker-ready")])
    script.wait()
    assert 230.0 <= script.now < 231.0


def test_start_failed_fails_at_once_even_while_the_process_still_runs() -> None:
    script = _Script([(0.0, _phase("roles")), (5.0, "aether-worker-start-failed phase=roles error=SystemExit")])
    with pytest.raises(AssertionError, match="start-failed phase=roles"):
        script.wait()
    assert script.now < 6.0


def test_no_progress_for_the_stall_limit_fails_and_names_the_last_phase() -> None:
    script = _Script([(1.0, _phase("role-ready role=verifier")), (500.0, "aether-worker-ready")])
    with pytest.raises(AssertionError, match=r"stall.*role-ready role=verifier"):
        script.wait()
    assert startup_budget.STALL_LIMIT < script.now < startup_budget.STALL_LIMIT + 2


def test_progress_past_the_total_budget_still_fails() -> None:
    steps = [(10.0 * index, _phase(f"step-{index}")) for index in range(200)]
    script = _Script(steps)
    with pytest.raises(AssertionError, match="total"):
        script.wait()
    assert startup_budget.TOTAL_BUDGET < script.now < startup_budget.TOTAL_BUDGET + 2


def test_image_preparation_is_not_counted_in_the_total_but_may_not_stall() -> None:
    # The image is prebuilt by the session fixture, so preparing it is a quick lookup. Its time is left
    # out of the total (which never involves the 3600 s build budget); a stuck preparation still trips
    # the stall limit like any other step.
    steps = [(70.0 + 68.0 * index, _phase(f"step-{index}")) for index in range(5)]
    script = _Script(
        [
            (0.0, _phase(startup_budget.IMAGE_PREP_START)),
            (70.0, _phase(startup_budget.IMAGE_PREP_END)),
            *steps,
            (70.0 + startup_budget.TOTAL_BUDGET - 5.0, "aether-worker-ready"),
        ]
    )
    script.wait()
    assert script.now > startup_budget.TOTAL_BUDGET
    stuck = _Script([(0.0, _phase(startup_budget.IMAGE_PREP_START)), (1000.0, _phase(startup_budget.IMAGE_PREP_END))])
    with pytest.raises(AssertionError, match=r"stall.*image-prep-start"):
        stuck.wait()
    assert stuck.now < startup_budget.STALL_LIMIT + 2


def test_an_exited_process_fails_at_once() -> None:
    script = _Script([(0.0, _phase("init"))], exit_at=3.0)
    with pytest.raises(AssertionError, match="退出"):
        script.wait()
    assert script.now < 4.0
