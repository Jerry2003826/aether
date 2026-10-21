"""How long worker startup may take, step by step, and the progress lines it prints on the way.

Kept free of heavy imports so a test helper can wait on a worker with the product's own budget.
Each bounded step prints an aether-worker-phase line; a start that fails before aether-worker-ready
prints one aether-worker-start-failed line naming the step it was in.
"""

from __future__ import annotations

import time

# One role process (verifier, gateway, runner) must print its ready line within this, then answer
# its health check within HEALTH_TIMEOUT. The roles are started and awaited one after another.
READY_TIMEOUT = 60.0
HEALTH_TIMEOUT = 15.0
ROLE_COUNT = 3
# A first OpenHands image build on a clean host; runs before any role starts and is not part of
# the role budget (a prebuilt image makes it a quick local lookup).
PREPARE_TIMEOUT = 3600.0
# Connecting to Temporal after the roles are up.
CONNECT_TIMEOUT = 30.0
# Restoring the started projects' indexes before the worker takes work.
RESTORE_BUDGET = 60.0
# The whole start, image preparation excluded.
TOTAL_BUDGET = ROLE_COUNT * (READY_TIMEOUT + HEALTH_TIMEOUT) + CONNECT_TIMEOUT + RESTORE_BUDGET
# The longest single bounded step: no new progress line for longer than this means a stuck start.
STALL_LIMIT = READY_TIMEOUT + HEALTH_TIMEOUT

PHASE_PREFIX = "aether-worker-phase "
FAILED_PREFIX = "aether-worker-start-failed "
READY_LINE = "aether-worker-ready"
IMAGE_PREP_START = "image-prep-start"
IMAGE_PREP_END = "image-prep-done"

_STARTED = time.monotonic()


def phase(name: str, **fields: str) -> None:
    extra = "".join(f" {key}={value}" for key, value in fields.items())
    print(f"{PHASE_PREFIX}name={name} elapsed={time.monotonic() - _STARTED:.2f}{extra}", flush=True)


def start_failed(name: str, exc: BaseException) -> None:
    print(f"{FAILED_PREFIX}phase={name} error={type(exc).__name__}", flush=True)
