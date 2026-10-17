"""A grading-integrity failure exits 1, so the worker records FAILED and keeps the agent's patch."""

from __future__ import annotations

import json
import subprocess

import pytest

from aether_worker import coding_sandbox
from aether_worker.coding_jobs import _coding_terminal, _patch_source, _published_patch

_PATCH = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 42\n"


def _result(detail: str) -> dict:
    return {
        "passed": 0,
        "failed": 1,
        "errors": 1,
        "cases": [{"name": "runner", "verdict": "FAIL", "detail": detail}],
        "network_errno": 101,
        "credential_present": False,
        "executor": "openhands.sdk.Agent",
        "input_tokens": 30,
        "output_tokens": 9,
        # The hook was written with the terminal tool, not file_editor.
        "edit_calls": 0,
        "model_requests": 2,
        "patch": _PATCH,
        "agent_completed": True,
    }


@pytest.mark.parametrize("detail", ["grading_startup_hook", "grading_shadow_module"])
def test_grading_violation_exit_1_is_failed_with_the_agent_patch(monkeypatch, detail: str) -> None:
    stdout = ("RESULT " + json.dumps(_result(detail)) + "\n").encode()
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(coding_sandbox, "container_logs", lambda _name, *, limit: (stdout[-limit:], b""))

    def docker(args, *, timeout=None):
        if args[:3] == ["inspect", "-f", "{{.State.ExitCode}}"]:
            return subprocess.CompletedProcess(args, 0, "1\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", docker)
    sandbox = coding_sandbox._collect_exit("aether-code-job1")
    assert sandbox["exit_code"] == 1
    assert _coding_terminal(sandbox, upstream_error=None, measured=(30, 9), usage_missing=False, channel_revoked=True) == ("FAILED", detail)
    assert _patch_source(sandbox) == "agent_edit"
    workspace = {"base_files": {"app.py": "def value():\n    return 0\n"}, "files": {"app.py": "def value():\n    return 0\n"}}
    assert _published_patch(workspace, sandbox) == _PATCH.encode()
