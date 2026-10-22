"""Wait for the Next.js dev server the console browser test starts.

The limits are the worker startup constants: no output for one stall slot means stuck, and
ROLE_COUNT stall slots is the total. The clock is time.monotonic(). A gap on that clock is
elapsed time, not a pause.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

from aether_worker import startup_budget


def wait_for_start(
    bucket: list[str],
    proc: subprocess.Popen[str],
    *,
    ready: str,
    failed: str,
    total: float,
    stall: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    started = clock()
    last_progress = started
    seen = 0
    last_line = "(none)"

    def fail(reason: str) -> AssertionError:
        return AssertionError(f"{reason}; last={last_line!r}: {''.join(bucket[-40:])}")

    while True:
        now = clock()
        while seen < len(bucket):
            line = bucket[seen].strip()
            seen += 1
            last_progress = now
            if line:
                last_line = line
            if line.startswith(failed):
                raise fail(f"start-failed {line.removeprefix(failed)}")
            if line == ready or line.startswith(ready + " "):
                return
        code = proc.poll()
        if code is not None:
            raise fail(f"进程提前退出 {code}")
        if now - last_progress > stall:
            raise fail(f"stall: no progress for {now - last_progress:.0f}s (limit {stall:.0f}s)")
        if now - started > total:
            raise fail(f"total startup {now - started:.0f}s over budget {total:.0f}s")
        sleep(0.1)


# The Next.js dev server has no product startup budget of its own. A slow but moving start is
# waited for; a stuck one is not. Both numbers are the worker constants, not a second copy.
WEB_STALL_LIMIT = startup_budget.STALL_SECONDS
WEB_TOTAL_BUDGET = startup_budget.ROLE_COUNT * startup_budget.STALL_SECONDS
WEB_READY_PREFIX = "✓ Ready in"
WEB_FAILED_PREFIX = "⨯"


def wait_web_ready(
    bucket: list[str],
    proc: subprocess.Popen[str],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    wait_for_start(
        bucket,
        proc,
        ready=WEB_READY_PREFIX,
        failed=WEB_FAILED_PREFIX,
        total=WEB_TOTAL_BUDGET,
        stall=WEB_STALL_LIMIT,
        clock=clock,
        sleep=sleep,
    )
