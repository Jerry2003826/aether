"""Without git, the published patch cannot be written; the job still ends with its own state and code.

unified_patch raises WorkspaceError("patch_unavailable") when git cannot run. The two places that
call it, _publish (through _published_patch, when the agent left no patch) and _publish_stop (a
cancel or deadline stop), must not crash or turn the job into FAILED/patch_unavailable: they store
the terminal state and error code they were given, with the unchanged marker as the patch and
patch_error recorded in the events.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from aether_worker import coding_jobs
from aether_worker.coding_workspace import (
    UNCHANGED_PATCH,
    WorkspaceError,
    unified_patch,
)

_WORKSPACE = {
    "base_files": {"app.py": "def value():\n    return 0\n"},
    "files": {"app.py": "def value():\n    return 42\n"},
    "tests": {"test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n"},
}
_ROW = {"id": "job-1", "task_id": "task-1", "operation_id": "op-1", "fencing_token": 3}


@pytest.fixture
def no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "bin-without-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(WorkspaceError) as missing:
        unified_patch(_WORKSPACE["base_files"], _WORKSPACE["files"])
    assert missing.value.code == "patch_unavailable"


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(coding_jobs, "_mark", lambda *args, **kwargs: None)

    def store(_database: object, _store: object, **kwargs: Any) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(coding_jobs, "_store_terminal", store)
    return calls


def _sandbox(patch: object) -> dict[str, Any]:
    return {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "result": {"passed": 0, "failed": 1, "agent_completed": True, "model_requests": 1, "edit_calls": 0, "patch": patch},
    }


@pytest.mark.parametrize(("state", "error_code"), [("FAILED", "agent_no_change"), ("FAILED", "test_failed"), ("FAILED", "model_not_called")])
def test_publish_without_git_keeps_its_state_and_error_code(
    no_git: None, stored: list[dict[str, Any]], state: str, error_code: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="aether.worker.coding")
    published = coding_jobs._publish(
        object(),
        object(),
        owner_id="owner",
        project_id="project",
        row=dict(_ROW),
        workspace=_WORKSPACE,
        sandbox=_sandbox(""),
        state=state,
        error_code=error_code,
        manifest=["app.py"],
    )
    assert published is True
    [call] = stored
    assert (call["state"], call["error_code"]) == (state, error_code)
    assert call["patch"] == UNCHANGED_PATCH.encode()
    events = json.loads(call["events"])
    assert events["error_code"] == error_code
    assert events["patch_error"] == "patch_unavailable"
    assert "patch_unavailable" in caplog.text


def test_publish_without_git_still_publishes_the_agents_own_patch(no_git: None, stored: list[dict[str, Any]]) -> None:
    agent = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 42\n"
    coding_jobs._publish(
        object(), object(), owner_id="owner", project_id="project", row=dict(_ROW), workspace=_WORKSPACE,
        sandbox=_sandbox(agent), state="FINISHED", error_code=None, manifest=["app.py"],
    )
    [call] = stored
    assert (call["state"], call["error_code"], call["patch"]) == ("FINISHED", None, agent.encode())
    assert "patch_error" not in json.loads(call["events"])


@pytest.mark.parametrize(
    ("state", "error_code", "cancel"),
    [("CANCELLED", "cancelled", True), ("FAILED", "deadline_exceeded", False), ("FAILED", "patch_too_large", False)],
)
def test_publish_stop_without_git_keeps_its_state_and_error_code(
    no_git: None, stored: list[dict[str, Any]], state: str, error_code: str, cancel: bool
) -> None:
    published = coding_jobs._publish_stop(
        object(),
        object(),
        owner_id="owner",
        project_id="project",
        row=dict(_ROW),
        workspace=_WORKSPACE,
        state=state,
        error_code=error_code,
        manifest=["app.py"],
        logs={"stdout": "out", "stderr": "err"},
        cancel=cancel,
    )
    assert published is True
    [call] = stored
    assert (call["state"], call["error_code"]) == (state, error_code)
    assert call["patch"] == UNCHANGED_PATCH.encode()
    events = json.loads(call["events"])
    assert events["error_code"] == error_code
    assert events["patch_error"] == "patch_unavailable"


def test_with_git_the_stop_publishes_the_candidate_patch_and_no_patch_error(stored: list[dict[str, Any]]) -> None:
    coding_jobs._publish_stop(
        object(), object(), owner_id="owner", project_id="project", row=dict(_ROW), workspace=_WORKSPACE,
        state="FAILED", error_code="deadline_exceeded", manifest=["app.py"], logs={}, cancel=False,
    )
    [call] = stored
    assert call["patch"].startswith(b"diff --git a/app.py b/app.py\n") and b"+    return 42" in call["patch"]
    assert "patch_error" not in json.loads(call["events"])
