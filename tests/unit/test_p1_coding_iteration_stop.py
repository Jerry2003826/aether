"""A conversation stopped by the iteration limit is not FINISHED (ADR-F47).

Even when the files on disk already pass (live run l31: the fix was in, the finish step was cut off),
the job is FAILED with its own reason, agent_iteration_limit, never the generic agent_interrupted,
so the cause is visible and nothing is silently counted as done.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.test_openhands_coding import _sandbox_shared_memory  # noqa: F401  (autouse fixture)
from tests.unit.test_p1_coding_iteration_limit import _ENV, _run_entry


def test_a_conversation_stopped_by_the_limit_says_so(tmp_path: Path) -> None:
    body, fake = _run_entry(tmp_path, {_ENV: "3"})
    assert len(fake.bodies) == 3
    assert body["agent_completed"] is False
    assert body["agent_stop"] == "iteration_limit"


def test_stopped_by_the_limit_is_failed_with_its_own_reason_even_when_tests_pass() -> None:
    from aether_worker.coding_jobs import _coding_terminal

    graded = {"result": {"passed": 1, "failed": 0, "agent_completed": False, "agent_stop": "iteration_limit", "model_requests": 4}, "exit_code": 0}
    assert _coding_terminal(graded, upstream_error=None, measured=(10, 10), usage_missing=False, channel_revoked=True) == (
        "FAILED",
        "agent_iteration_limit",
    )
    other = {"result": {"passed": 1, "failed": 0, "agent_completed": False, "model_requests": 4}, "exit_code": 0}
    assert _coding_terminal(other, upstream_error=None, measured=(10, 10), usage_missing=False, channel_revoked=True) == (
        "FAILED",
        "agent_interrupted",
    )

