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


def _run(steps: list[tuple[float, str | None]], proc: _Proc | None = None) -> None:
    bucket: list[str] = []
    script = _Script(steps, bucket)
    sb.wait_worker_ready(bucket, proc or _Proc(), now=script.now, sleep=script.sleep)


def test_budget_constants_match_the_design() -> None:
    assert sb.STALL_SECONDS == sb.ROLE_READY_SECONDS + sb.ROLE_HEALTH_SECONDS
    assert sb.ROLE_SECONDS == sb.STALL_SECONDS
    assert sb.TOTAL_SECONDS == sb.ROLE_COUNT * sb.ROLE_SECONDS + sb.TEMPORAL_CONNECT_SECONDS + sb.WORKER_REST_SECONDS


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
    with pytest.raises(sb.WorkerStartFailed, match=f"超过 {sb.TOTAL_SECONDS:.0f}s"):
        _run(steps)


def test_image_preparation_is_outside_the_total() -> None:
    prep = [(4.0, f"prepare {i}") for i in range(100)]  # 400s of progressing image prep
    rest = [(0.1, "aether-worker-phase step=image-ready"), (4.0, "phase roles"), (0.1, "aether-worker-ready")]
    _run(prep + rest)


def test_a_monotonic_gap_is_a_stall_even_when_a_later_line_would_be_ready() -> None:
    """time.monotonic() does not move while the box is paused, so a gap in it is real elapsed time.

    Treating that gap as a pause would let a blocked step reset the stall budget. The ready line
    after the gap must not be reached.
    """
    steps = [
        (0.1, "aether-worker-phase step=image-ready"),
        (sb.STALL_SECONDS + 1, None),
        (0.1, "aether-worker-ready"),
    ]
    with pytest.raises(sb.WorkerStartFailed, match="停滞"):
        _run(steps)
