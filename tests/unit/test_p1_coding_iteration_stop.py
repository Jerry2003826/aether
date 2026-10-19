"""A conversation stopped by the iteration limit after the tests pass is FINISHED, and says so (ADR-F47).

Live run l31: the fix was in and the tests passed, but the step limit cut off the finish step and the
job was FAILED/agent_interrupted. Owner decision (2026-10-19): that job is FINISHED. It is never a
silent difference: the entry reports agent_stop="iteration_limit" (with the agent's usage and
request count), the stored test result and the coding_job log line carry it, and the stop only
finishes a job the rest of the verdict would finish (agent change, passing tests, channel closed,
no budget or upstream error). Without a passing change it is FAILED/agent_iteration_limit.

The other two interruption reasons keep their outcomes: a budget refusal stays FAILED/budget_exceeded
(admission) or agent_interrupted (refused mid-conversation), and a revoked or still-open model channel
stays agent_interrupted or model_channel_open.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aether_worker import coding_jobs
from aether_worker.coding_jobs import _coding_terminal

from tests.unit.test_openhands_coding import _sandbox_shared_memory  # noqa: F401  (autouse fixture)
from tests.unit.test_p1_coding_iteration_limit import _ENV, _run_entry
from tests.unit.test_r10_model_channel_worker import _Job, _revoke_in_band

_PATCH = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 42\n"


def _sandbox(*, passed: bool = True, stop: str | None = "iteration_limit", completed: bool = False, patch: str = _PATCH) -> dict:
    body: dict[str, Any] = {
        "passed": 1 if passed else 0,
        "failed": 0 if passed else 1,
        "errors": 0,
        "agent_completed": completed,
        "model_requests": 5,
        "edit_calls": 1,
        "patch": patch,
        "cases": [{"name": "test_app.test_value", "verdict": "PASS" if passed else "FAIL", "detail": ""}],
    }
    if stop is not None:
        body["agent_stop"] = stop
    return {"exit_code": 0 if passed else 1, "result": body}


def _verdict(sandbox: dict, **overrides: Any) -> tuple[str, str | None]:
    arguments: dict[str, Any] = {"upstream_error": None, "measured": (100, 20), "usage_missing": False, "channel_revoked": True}
    arguments.update(overrides)
    return _coding_terminal(sandbox, **arguments)


def test_a_conversation_stopped_by_the_limit_says_so(tmp_path: Path) -> None:
    # Five working steps (the edit is step 4), no room for the finish call.
    body, fake = _run_entry(tmp_path, {_ENV: "5"})
    assert len(fake.bodies) == 5
    assert body["agent_completed"] is False
    assert body["agent_stop"] == "iteration_limit"
    assert body["model_requests"] == 5
    assert body["edit_calls"] == 1
    assert "return 42" in body["patch"]
    assert body["passed"] == 1


def test_stopped_by_the_limit_after_the_tests_pass_is_finished() -> None:
    assert _verdict(_sandbox()) == ("FINISHED", None)


def test_stopped_by_the_limit_without_a_passing_change_is_failed_with_its_own_reason() -> None:
    assert _verdict(_sandbox(passed=False)) == ("FAILED", "agent_iteration_limit")
    assert _verdict(_sandbox(patch="")) == ("FAILED", "agent_iteration_limit")


def test_other_unfinished_conversations_are_still_interrupted() -> None:
    assert _verdict(_sandbox(stop=None)) == ("FAILED", "agent_interrupted")
    assert _verdict(_sandbox(stop="something_else")) == ("FAILED", "agent_interrupted")


def test_budget_interruptions_keep_their_outcome() -> None:
    # Refused at admission: budget_exceeded, whether or not the limit also stopped the agent.
    assert _verdict(_sandbox(), admission_error="budget_exceeded") == ("FAILED", "budget_exceeded")
    assert _verdict(_sandbox(stop=None), admission_error="budget_exceeded") == ("FAILED", "budget_exceeded")
    # Refused mid-conversation (the agent raised on the 409): still agent_interrupted.
    assert _verdict(_sandbox(stop=None), upstream_error="budget_exceeded") == ("FAILED", "agent_interrupted")
    # The limit does not hide an upstream refusal either.
    assert _verdict(_sandbox(), upstream_error="budget_exceeded") == ("FAILED", "budget_exceeded")


def test_revoke_interruptions_keep_their_outcome() -> None:
    # Graded without the in-band revoke: model_channel_open, even after an iteration-limit stop.
    assert _verdict(_sandbox(), channel_revoked=False) == ("FAILED", "model_channel_open")
    assert _verdict(_sandbox(stop=None), channel_revoked=False) == ("FAILED", "model_channel_open")
    # The channel was revoked under the agent (its next call got 401): agent_interrupted.
    assert _verdict(_sandbox(stop=None), upstream_error="revoked") == ("FAILED", "agent_interrupted")


class _StoppedJob(_Job):
    """A whole coding job whose sandbox reports an iteration-limit stop after a passing change."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch, revoke=True)
        self.stored: list[dict] = []
        job = self
        monkeypatch.setattr(
            coding_jobs,
            "_publish",
            lambda *_a, **k: job.published.append((k["state"], k["error_code"])) or job.stored.append(dict(k["sandbox"]["result"])) or True,
        )

    def sandbox(self, _root: Path, *, proxy_dir: Path, job_token: str, **_kwargs: Any) -> dict[str, Any]:
        assert _revoke_in_band(proxy_dir / "model.sock", job_token) == 200
        return _sandbox()


def test_a_job_stopped_by_the_limit_after_passing_is_finished_and_records_the_stop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    job = _StoppedJob(monkeypatch)
    job.run()
    assert job.published == [("FINISHED", None)]
    assert job.stored and job.stored[0]["agent_stop"] == "iteration_limit"
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("coding_job job=")]
    assert lines and "state=FINISHED" in lines[-1] and "agent_stop=iteration_limit" in lines[-1], lines
