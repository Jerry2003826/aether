"""patch_too_large: a terminal FAILED job, not requeued, distinct from agent_no_change, logged with the line size."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from aether_worker import coding_jobs
from aether_worker.coding_jobs import PATCH_TOO_LARGE, _coding_terminal, _log_job
from test_r10_entry_result import _entry
from test_r10_model_channel_worker import _Job

_RAW = 1_900_000
_BODY_FIELDS = (
    "{'passed': 1, 'failed': 0, 'errors': 0, 'cases': [{'name': 'test_app.test_value', 'verdict': 'PASS', 'detail': ''}],"
    " 'agent_completed': True, 'model_requests': 1, 'patch': patch}"
)
_PATCH = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n+" + "\x01" * _RAW + "\n"
_OMITTED = {
    "passed": 1,
    "failed": 0,
    "errors": 0,
    "cases": [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}],
    "agent_completed": True,
    "model_requests": 1,
    "patch": "",
    "patch_omitted": "too_large",
    "result_line_bytes": 11_400_208,
}


def test_the_entry_reports_the_serialized_length_of_the_line_it_could_not_send(
    tmp_path: Path,
) -> None:
    snippet = f"patch = '--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n+' + '\\x01' * {_RAW} + '\\n'\nentry.emit({_BODY_FIELDS}, 0)\n"
    completed = _entry(snippet, tmp_path)
    assert completed.returncode == 0, completed.stderr[-2000:]
    line = completed.stdout.rstrip(b"\n").rsplit(b"\n", 1)[-1]
    body = json.loads(line[len(b"RESULT ") :])
    full = {
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "cases": [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}],
        "agent_completed": True,
        "model_requests": 1,
        "patch": _PATCH,
    }
    expected = len(("RESULT " + json.dumps(full, separators=(",", ":"))).encode())
    assert body["patch_omitted"] == "too_large"
    assert body["result_line_bytes"] == expected


def test_the_job_log_line_carries_the_serialized_result_length(caplog) -> None:
    caplog.set_level(logging.INFO, logger="aether.worker.coding")
    _log_job(
        job_id="job-big",
        fencing=3,
        state="FAILED",
        error=PATCH_TOO_LARGE,
        result=dict(_OMITTED),
        measured=(3, 4),
        patch_source="candidate_files",
    )
    assert "error=patch_too_large" in caplog.text
    assert "result_line_bytes=11400208" in caplog.text


def test_patch_too_large_is_its_own_terminal_code_not_agent_no_change() -> None:
    sandbox = {"exit_code": 0, "result": dict(_OMITTED)}
    assert _coding_terminal(
        sandbox,
        upstream_error=None,
        measured=(3, 4),
        usage_missing=False,
        channel_revoked=True,
    ) == (
        "FAILED",
        PATCH_TOO_LARGE,
    )
    # Without the marker the same empty patch is agent_no_change: the two stay distinguishable.
    plain = {
        "exit_code": 0,
        "result": {
            key: value
            for key, value in _OMITTED.items()
            if key not in {"patch_omitted", "result_line_bytes"}
        },
    }
    assert _coding_terminal(
        plain,
        upstream_error=None,
        measured=(3, 4),
        usage_missing=False,
        channel_revoked=True,
    ) == (
        "FAILED",
        "agent_no_change",
    )
    assert PATCH_TOO_LARGE not in coding_jobs.GRADING_VIOLATIONS


class _TooLarge(_Job):
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch, revoke=True)
        self.sandbox_runs = 0
        self.claims = 0
        harness = self
        real_claim = coding_jobs._claim

        def claim(*args: Any, **kwargs: Any) -> Any:
            harness.claims += 1
            return real_claim(*args, **kwargs)

        monkeypatch.setattr(coding_jobs, "_claim", claim)

    def sandbox(self, root: Path, **kwargs: Any) -> dict[str, Any]:
        self.sandbox_runs += 1
        super().sandbox(root, **kwargs)
        return {"exit_code": 0, "result": dict(_OMITTED)}


def test_a_too_large_patch_ends_the_job_failed_without_another_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="aether.worker.coding")
    job = _TooLarge(monkeypatch)
    outcome = job.run()
    # Terminal: FAILED is published, the attempt is not handed back (no ACTIVE/fence_lost), nothing reruns it.
    assert outcome == {"coding_job_id": outcome["coding_job_id"], "state": "FAILED"}
    assert job.published == [("FAILED", PATCH_TOO_LARGE)]
    assert job.sandbox_runs == 1
    assert job.claims == 1
    assert "error=patch_too_large" in caplog.text
    assert "result_line_bytes=11400208" in caplog.text
