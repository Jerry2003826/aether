"""The coding agent's per-job step limit: one config source for the worker, the gateway and the sandbox.

One agent iteration is one model call, so the same number is the sandbox entry's
max_iteration_per_run (the worker hands it to the container as AETHER_CODING_MAX_ITERATIONS; the
entry cannot import this package and has no default of its own) and the gateway's per-job
coding-call cap. They can never silently differ. What a job may spend stays bounded by its money,
token and request budget; this limit only stops a conversation that never ends.
"""

from __future__ import annotations

import os
import re

CODING_ITERATIONS_ENV = "AETHER_CODING_MAX_ITERATIONS"
CODING_MAX_ITERATIONS = 30
CODING_ITERATIONS_CEILING = 200
_DIGITS = re.compile(r"[0-9]+")


def coding_max_iterations() -> int:
    """AETHER_CODING_MAX_ITERATIONS, or 30 when unset or blank. Anything other than an integer from
    1 to 200 raises ValueError: an invalid limit is refused, never replaced by the default."""
    raw = os.environ.get(CODING_ITERATIONS_ENV, "").strip()
    if not raw:
        return CODING_MAX_ITERATIONS
    if _DIGITS.fullmatch(raw) is None or not 1 <= int(raw) <= CODING_ITERATIONS_CEILING:
        raise ValueError(f"{CODING_ITERATIONS_ENV} must be an integer from 1 to {CODING_ITERATIONS_CEILING}")
    return int(raw)


def require_coding_limits() -> None:
    """Startup check for the worker and the gateway: an invalid limit stops the process with a clear message."""
    try:
        coding_max_iterations()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
