"""candidate_files_too_large is its own failure code, not test_failed: nothing of the candidate ran."""

from __future__ import annotations

from aether_worker.coding_jobs import _coding_terminal


def _sandbox(case: dict) -> dict:
    result = {
        "passed": 0,
        "failed": 1,
        "errors": 0,
        "cases": [case],
        "network_errno": 101,
        "credential_present": False,
        "input_tokens": 30,
        "output_tokens": 9,
        "patch": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n",
        "agent_completed": True,
    }
    return {"exit_code": 1, "result": result}


def _terminal(sandbox: dict) -> tuple[str, str | None]:
    return _coding_terminal(sandbox, upstream_error=None, measured=(30, 9), usage_missing=False, channel_revoked=True)


def test_candidate_files_too_large_has_its_own_code() -> None:
    sandbox = _sandbox({"name": "suite", "verdict": "FAIL", "detail": "candidate_files_too_large"})
    assert _terminal(sandbox) == ("FAILED", "candidate_files_too_large")


def test_an_ordinary_failure_stays_test_failed() -> None:
    sandbox = _sandbox({"name": "test_app.test_value", "verdict": "FAIL", "detail": "assert 0 == 1"})
    assert _terminal(sandbox) == ("FAILED", "test_failed")


def test_a_test_named_like_the_code_is_not_the_suite_failure() -> None:
    sandbox = _sandbox({"name": "test_app.test_value", "verdict": "FAIL", "detail": "candidate_files_too_large"})
    assert _terminal(sandbox) == ("FAILED", "test_failed")
