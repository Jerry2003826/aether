"""A sandbox without git cannot write the agent's patch: its own error code, never agent_no_change.

The entry writes the published patch with git (as the worker's unified_patch does). Without git there
is no patch to publish, which says nothing about whether the agent changed anything, so the job must
not end FAILED/agent_no_change (or be graded): the entry reports runner detail `patch_unavailable`
(exit 2, before grading) and the worker records FAILED/patch_unavailable, the code the worker's own
unified_patch uses when git is missing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from aether_worker import coding_sandbox
from aether_worker.coding_sandbox import CodingSandboxError

_ENTRY_DIR = Path("services/worker/aether_worker").resolve()
_PYTHON = Path("environments/openhands/.venv/bin/python")


def test_the_entry_without_git_reports_patch_unavailable_before_grading(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    job = tmp_path / "job"
    job.mkdir()
    (job / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    shm = tmp_path / "shm"
    shm.mkdir()
    code = f"""
import sys
sys.path.append({str(_ENTRY_DIR)!r})
import openhands_sandbox_entry as entry
from pathlib import Path
job = Path({str(job)!r})

def run_agent(_job, _objective, _iterations):
    # The agent did change a source file.
    (job / "app.py").write_text("def value():\\n    return 2\\n", encoding="utf-8")
    report = entry._blank_report()
    report.update(agent_completed=True, model_requests=1, edit_calls=1)
    return report

def graded():
    # Reaching the model-channel close means grading would run: not without a patch.
    sys.exit(97)

entry.become_subreaper = lambda: None
entry.job_objective = lambda: "objective"
entry.iteration_limit = lambda: 30
entry.shared_memory_root = lambda: Path({str(shm)!r})
entry.job_directory = lambda: job
entry.copy_tree = lambda _src, _dst: None
entry._run_agent = run_agent
entry.stop_agent_processes = lambda: None
entry.clear_shared_memory = lambda _root: None
entry.close_model_channel = graded
entry.main()
"""
    completed = subprocess.run(
        [str(_PYTHON.absolute()), "-I", "-c", code],
        cwd=tmp_path,
        # No git anywhere on PATH.
        env={"PATH": str(tmp_path / "no-git"), "HOME": str(tmp_path), "AETHER_SANDBOX_WORK": str(tmp_path / "work")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
    assert completed.returncode == 2, (completed.returncode, completed.stdout[-2000:], completed.stderr[-2000:])
    assert len(lines) == 1, completed.stdout[-2000:]
    body = json.loads(lines[0][len("RESULT ") :])
    assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": "patch_unavailable"}], body
    assert body["patch"] == "", body


def test_the_worker_records_patch_unavailable_not_sandbox_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "passed": 0,
        "failed": 1,
        "errors": 1,
        "cases": [{"name": "runner", "verdict": "FAIL", "detail": "patch_unavailable"}],
        "patch": "",
        "agent_completed": False,
    }
    stdout = "RESULT " + json.dumps(body) + "\n"
    monkeypatch.setattr(coding_sandbox, "_bounded_logs", lambda _name, **_kwargs: {"stdout": stdout, "stderr": ""})
    monkeypatch.setattr(coding_sandbox, "_exit_code", lambda _name: 2)
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    with pytest.raises(CodingSandboxError) as caught:
        coding_sandbox._collect_exit("aether-coding-job")
    assert caught.value.code == "patch_unavailable"


@pytest.mark.parametrize("detail", ["objective_missing", "shared_memory_unconfigured", "RuntimeError"])
def test_other_runner_failures_stay_sandbox_failed(monkeypatch: pytest.MonkeyPatch, detail: str) -> None:
    body = {"passed": 0, "failed": 1, "errors": 1, "cases": [{"name": "runner", "verdict": "FAIL", "detail": detail}]}
    stdout = "RESULT " + json.dumps(body) + "\n"
    monkeypatch.setattr(coding_sandbox, "_bounded_logs", lambda _name, **_kwargs: {"stdout": stdout, "stderr": ""})
    monkeypatch.setattr(coding_sandbox, "_exit_code", lambda _name: 2)
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    with pytest.raises(CodingSandboxError) as caught:
        coding_sandbox._collect_exit("aether-coding-job")
    assert caught.value.code == "sandbox_failed"
