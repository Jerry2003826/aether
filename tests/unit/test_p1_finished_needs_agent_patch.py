"""A coding job is FINISHED only with the agent's own patch; the worker's git diff never makes it so.

The worker writes a candidate patch with git (unified_patch) from the uploaded base and candidate
files. That patch describes the upload, not an agent change: when the agent left no patch, the job
stays FAILED/agent_no_change even though the worker's diff is a real change and the tests pass. When
the job is FINISHED, the published patch is the agent's patch, never the worker's git diff.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aether_worker import coding_jobs
from aether_worker.coding_workspace import unified_patch

_WORKSPACE = {
    "base_files": {"app.py": "def value():\n    return 0\n"},
    "files": {"app.py": "def value():\n    return 42\n"},
    "tests": {"test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n"},
}
_ROW = {"id": "job-1", "task_id": "task-1", "operation_id": "op-1", "fencing_token": 3}
_AGENT = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 7\n"


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(coding_jobs, "_mark", lambda *args, **kwargs: None)

    def store(_database: object, _store: object, **kwargs: Any) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(coding_jobs, "_store_terminal", store)
    return calls


def _sandbox(**result: Any) -> dict[str, Any]:
    body = {"passed": 1, "failed": 0, "agent_completed": True, "model_requests": 1, "edit_calls": 1}
    body.update(result)
    return {"exit_code": 0, "stdout": "", "stderr": "", "result": body}


def _worker_patch() -> str:
    patch = unified_patch(_WORKSPACE["base_files"], _WORKSPACE["files"])
    assert patch.startswith("diff --git a/app.py b/app.py\n") and "+    return 42" in patch
    return patch


def _terminal(sandbox: dict[str, Any]) -> tuple[str, str | None]:
    return coding_jobs._coding_terminal(sandbox, upstream_error=None, measured=(8, 4), usage_missing=False, channel_revoked=True)


def _publish(sandbox: dict[str, Any], state: str, error_code: str | None) -> None:
    published = coding_jobs._publish(
        object(), object(), owner_id="owner", project_id="project", row=dict(_ROW), workspace=_WORKSPACE,
        sandbox=sandbox, state=state, error_code=error_code, manifest=["app.py"],
    )
    assert published is True


@pytest.mark.parametrize("absent", ["missing", "", None])
def test_without_the_agents_patch_the_workers_git_diff_does_not_make_the_job_finished(
    stored: list[dict[str, Any]], absent: object
) -> None:
    worker_patch = _worker_patch()
    sandbox = _sandbox() if absent == "missing" else _sandbox(patch=absent)
    assert coding_jobs._agent_patch(sandbox) is None
    assert coding_jobs._patch_source(sandbox) == "candidate_files"
    state, error_code = _terminal(sandbox)
    assert (state, error_code) == ("FAILED", "agent_no_change")
    _publish(sandbox, state, error_code)
    [call] = stored
    assert (call["state"], call["error_code"]) == ("FAILED", "agent_no_change")
    assert call["patch"] == worker_patch.encode()
    events = json.loads(call["events"])
    assert events["error_code"] == "agent_no_change"
    assert "patch_error" not in events


def test_a_finished_job_publishes_the_agents_patch_not_the_workers_git_diff(stored: list[dict[str, Any]]) -> None:
    worker_patch = _worker_patch()
    sandbox = _sandbox(patch=_AGENT)
    assert coding_jobs._patch_source(sandbox) == "agent_edit"
    state, error_code = _terminal(sandbox)
    assert (state, error_code) == ("FINISHED", None)
    _publish(sandbox, state, error_code)
    [call] = stored
    assert (call["state"], call["error_code"]) == ("FINISHED", None)
    assert call["patch"] == _AGENT.encode()
    assert call["patch"] != worker_patch.encode()
    assert b"return 42" not in call["patch"]
    assert "patch_error" not in json.loads(call["events"])
