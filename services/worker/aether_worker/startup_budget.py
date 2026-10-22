"""Worker start budgets and the readiness wait that tests (and operators) use.

Deadlines are time.monotonic(). That clock does not move while the machine is suspended, so a
pause is not counted and is not something the wait has to detect. A gap between two reads is
elapsed time: a blocked call is a stall, not a pause. Limits are never raised to absorb a gap.
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
POLL_SECONDS = 0.1

PHASE = "aether-worker-phase"
START_FAILED = "aether-worker-start-failed"
READY = "aether-worker-ready"
IMAGE_READY_STEP = "image-ready"


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
) -> None:
    """Return once READY is in bucket; fail at once on START_FAILED or exit, on a stall, or past the total.

    `now` is a monotonic clock. The total starts after the image-ready phase (image preparation is
    outside the start budget); the stall check covers the whole start.
    """
    seen = 0
    last_progress = now()
    total_from: float | None = None
    while True:
        current = now()
        lines = list(bucket)
        if len(lines) > seen:
            for line in lines[seen:]:
                if READY in line:
                    return
                if START_FAILED in line:
                    raise WorkerStartFailed(f"worker 启动失败: {line.strip()} | {''.join(lines[-40:])}")
                if total_from is None and f"{PHASE} step={IMAGE_READY_STEP}" in line:
                    total_from = current
            seen = len(lines)
            last_progress = current
        if proc.poll() is not None:
            raise WorkerStartFailed(f"worker 启动中退出 {proc.returncode}: {''.join(lines[-40:])}")
        if current - last_progress > stall_seconds:
            raise WorkerStartFailed(f"worker 启动停滞 {stall_seconds:.0f}s 无新输出: {''.join(lines[-40:])}")
        if total_from is not None and current - total_from > total_seconds:
            raise WorkerStartFailed(f"worker 启动超过 {total_seconds:.0f}s: {''.join(lines[-40:])}")
        sleep(POLL_SECONDS)
