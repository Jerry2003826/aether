"""Waiting for a started AETHER process to report ready, with the product's own startup budget.

The worker prints an aether-worker-phase line at each startup step (aether_worker.startup_budget).
The wait returns at aether-worker-ready and fails:
- at once on aether-worker-start-failed or when the process exits;
- when no new line arrives for STALL_LIMIT (the longest single bounded step);
- when the start overruns TOTAL_BUDGET (3 roles x (ready 60 s + health 15 s), Temporal connect,
  index restore). Image preparation is left out of the total: the session fixture prebuilds the
  image, so it never involves the 3600 s build budget; a stuck preparation still trips the stall limit.
No wait here is unbounded and none is a bare fixed timeout.
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
    image_start: str | None = None,
    image_end: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    started = clock()
    last_progress = started
    seen = 0
    last_line = "(none)"
    image_since: float | None = None
    excluded = 0.0

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
            if image_start and image_start in line:
                image_since = now
            elif image_end and image_end in line and image_since is not None:
                excluded += now - image_since
                image_since = None
        code = proc.poll()
        if code is not None:
            raise fail(f"进程提前退出 {code}")
        if now - last_progress > stall:
            raise fail(f"stall: no progress for {now - last_progress:.0f}s (limit {stall:.0f}s)")
        counted = now - started - excluded - (now - image_since if image_since is not None else 0.0)
        if counted > total:
            raise fail(f"total startup {counted:.0f}s over budget {total:.0f}s")
        sleep(0.1)


def wait_worker_ready(
    bucket: list[str],
    proc: subprocess.Popen[str],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    wait_for_start(
        bucket,
        proc,
        ready=startup_budget.READY_LINE,
        failed=startup_budget.FAILED_PREFIX,
        total=startup_budget.TOTAL_BUDGET,
        stall=startup_budget.STALL_LIMIT,
        image_start=f"name={startup_budget.IMAGE_PREP_START} ",
        image_end=f"name={startup_budget.IMAGE_PREP_END} ",
        clock=clock,
        sleep=sleep,
    )


# The Next.js dev server has no product startup budget of its own; the console test gives it the
# worker's stall limit (no output for that long means stuck) and one role slot per step it prints
# (starting, compiling, ready), so a slow but moving start is waited for and a stuck one is not.
WEB_STALL_LIMIT = startup_budget.STALL_LIMIT
WEB_TOTAL_BUDGET = startup_budget.ROLE_COUNT * startup_budget.STALL_LIMIT
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
