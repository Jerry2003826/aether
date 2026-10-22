"""Worker start budgets and the readiness wait that tests (and operators) use.

Every limit here is counted in *active* time: time.monotonic() steps between two polls are
added only up to MAX_POLL_STEP_SECONDS. A longer step means the waiting process itself was not
running (VM pause, host suspend, SIGSTOP); the started process was frozen with it, so that gap is
neither a stall nor part of the start. Limits are never raised to absorb such a gap.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence

ROLE_READY_SECONDS = 60.0
ROLE_HEALTH_SECONDS = 15.0
ROLE_SECONDS = ROLE_READY_SECONDS + ROLE_HEALTH_SECONDS
TEMPORAL_CONNECT_SECONDS = 30.0
ROLE_COUNT = 3
WORKER_REST_SECONDS = 60.0
STALL_SECONDS = ROLE_SECONDS
TOTAL_SECONDS = ROLE_COUNT * ROLE_SECONDS + TEMPORAL_CONNECT_SECONDS + WORKER_REST_SECONDS
MAX_POLL_STEP_SECONDS = 5.0
POLL_SECONDS = 0.1

PHASE = "aether-worker-phase"
START_FAILED = "aether-worker-start-failed"
READY = "aether-worker-ready"
IMAGE_READY_STEP = "image-ready"


class ActiveClock:
    """Seconds this process was actually running, from a monotonic source; long gaps count as one poll."""

    def __init__(self, now: Callable[[], float] = time.monotonic, max_step: float = MAX_POLL_STEP_SECONDS) -> None:
        self._now = now
        self._max_step = max_step
        self._last = now()
        self.elapsed = 0.0
        self.suspended = 0.0

    def tick(self, allow: float = 0.0) -> float:
        """Advance; allow is how long the caller may legitimately have blocked since the last tick."""
        current = self._now()
        step = max(0.0, current - self._last)
        self._last = current
        if step > max(self._max_step, allow + self._max_step):
            self.suspended += step - POLL_SECONDS
            step = POLL_SECONDS
        self.elapsed += step
        return self.elapsed


class WorkerStartFailed(AssertionError):
    pass


def wait_worker_ready(
    bucket: Sequence[str],
    proc: subprocess.Popen[str],
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stall_seconds: float = STALL_SECONDS,
    total_seconds: float = TOTAL_SECONDS,
) -> ActiveClock:
    """Return once READY is in bucket; fail at once on START_FAILED or exit, on a stall, or past the total.

    The total starts after the image-ready phase (image preparation is outside the start budget);
    the stall check covers the whole start.
    """
    clock = ActiveClock(now)
    seen = 0
    last_progress = 0.0
    total_from: float | None = None
    while True:
        elapsed = clock.tick()
        lines = list(bucket)
        if len(lines) > seen:
            for line in lines[seen:]:
                if READY in line:
                    return clock
                if START_FAILED in line:
                    raise WorkerStartFailed(f"worker 启动失败: {line.strip()} | {''.join(lines[-40:])}")
                if total_from is None and f"{PHASE} step={IMAGE_READY_STEP}" in line:
                    total_from = elapsed
            seen = len(lines)
            last_progress = elapsed
        if proc.poll() is not None:
            raise WorkerStartFailed(f"worker 启动中退出 {proc.returncode}: {''.join(lines[-40:])}")
        if elapsed - last_progress > stall_seconds:
            raise WorkerStartFailed(f"worker 启动停滞 {stall_seconds:.0f}s 无新输出: {''.join(lines[-40:])}")
        if total_from is not None and elapsed - total_from > total_seconds:
            raise WorkerStartFailed(f"worker 启动超过 {total_seconds:.0f}s: {''.join(lines[-40:])}")
        sleep(POLL_SECONDS)
