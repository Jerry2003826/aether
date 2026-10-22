"""wait_worker_ready and the worker start budgets, on a scripted clock (no real time)."""

from __future__ import annotations

import pytest

from aether_worker import startup_budget as sb


class _Proc:
    def __init__(self, code: int | None = None) -> None:
        self.returncode = code

    def poll(self) -> int | None:
        return self.returncode


class _Script:
    """A monotonic clock and a log that advance together: each sleep runs the next scripted step."""

    def __init__(self, steps: list[tuple[float, str | None]], bucket: list[str]) -> None:
        self.now_value = 1000.0
        self.steps = list(steps)
        self.bucket = bucket

    def now(self) -> float:
        return self.now_value

    def sleep(self, _seconds: float) -> None:
        if not self.steps:
            self.now_value += 1.0
            return
        advance, line = self.steps.pop(0)
        self.now_value += advance
        if line is not None:
            self.bucket.append(line + "\n")


def _run(steps: list[tuple[float, str | None]], proc: _Proc | None = None) -> sb.ActiveClock:
    bucket: list[str] = []
    script = _Script(steps, bucket)
    return sb.wait_worker_ready(bucket, proc or _Proc(), now=script.now, sleep=script.sleep)


def test_budget_constants_match_the_design() -> None:
    assert sb.ROLE_READY_SECONDS == 60.0
    assert sb.ROLE_HEALTH_SECONDS == 15.0
    assert sb.TEMPORAL_CONNECT_SECONDS == 30.0
    assert sb.STALL_SECONDS == 75.0
    assert sb.TOTAL_SECONDS == 3 * (60 + 15) + 30 + 60


def test_returns_on_ready() -> None:
    _run([(0.1, "aether-worker-phase step=image-ready"), (0.1, "aether-worker-ready")])


def test_start_failed_fails_at_once() -> None:
    with pytest.raises(sb.WorkerStartFailed, match="phase=temporal-connect"):
        _run([(0.1, "aether-worker-start-failed phase=temporal-connect")])


def test_process_exit_fails_at_once() -> None:
    with pytest.raises(sb.WorkerStartFailed, match="退出 3"):
        _run([(0.1, None)], proc=_Proc(3))


def test_no_output_for_the_stall_budget_fails() -> None:
    steps = [(0.1, "aether-worker-phase step=image-ready")] + [(1.0, None)] * 80
    with pytest.raises(sb.WorkerStartFailed, match="停滞"):
        _run(steps)


def test_progress_past_the_total_fails() -> None:
    steps = [(0.1, "aether-worker-phase step=image-ready")] + [(4.0, f"progress {i}") for i in range(90)]
    with pytest.raises(sb.WorkerStartFailed, match="超过 315s"):
        _run(steps)


def test_image_preparation_is_outside_the_total() -> None:
    prep = [(4.0, f"prepare {i}") for i in range(100)]  # 400s of progressing image prep
    rest = [(0.1, "aether-worker-phase step=image-ready"), (4.0, "phase roles"), (0.1, "aether-worker-ready")]
    _run(prep + rest)


def test_a_suspended_box_is_neither_a_stall_nor_start_time() -> None:
    """The box paused for 2h40m in the middle of a progressing start (monotonic jumped 9600s)."""
    steps = [(0.1, "aether-worker-phase step=image-ready"), (4.0, "role verifier"), (9600.0, None), (4.0, "role runner")]
    steps += [(0.1, "aether-worker-ready")]
    clock = _run(steps)
    assert clock.elapsed < 20
    assert clock.suspended > 9000
