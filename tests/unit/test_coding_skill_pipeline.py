"""Coding-job workspace rules and skill metrics stay finite before any sandbox starts."""

from __future__ import annotations

import json
import logging
import math

import pytest

from aether_worker.bundle_files import BundleError, parse_file_map
from aether_worker.coding_jobs import _coding_terminal, _log_job, _patch_source, _published_patch
from aether_worker.coding_sandbox import CODING_MEMORY_BYTES, container_arguments
from aether_worker.coding_workspace import WorkspaceError, parse_workspace, unified_patch
from aether_worker.skill_dataset import DatasetError, parse_candidate, parse_dataset
from aether_worker.skill_eval import SkillEvalError, judge
from aether_worker.skill_execute import require_development_split
from aether_worker.skill_metrics import metrics_from_grades


def _workspace() -> dict:
    return {
        "kind": "aether.coding_workspace",
        "base_files": {"app.py": "def value():\n    return 0\n"},
        "files": {"app.py": "def value():\n    return 42\n"},
        "tests": {"test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n"},
    }


def test_lone_surrogate_is_rejected_as_an_invalid_bundle() -> None:
    body = _workspace()
    body["files"] = {"app.py": "def value():\n    return '\ud800'\n"}
    with pytest.raises(WorkspaceError) as rejected:
        parse_workspace(json.dumps(body).encode())
    assert rejected.value.code == "workspace_invalid"
    with pytest.raises(BundleError) as bundle:
        parse_file_map({"app.py": "\ud800"})
    assert bundle.value.code == "bundle_invalid"
    with pytest.raises(BundleError) as named:
        parse_file_map({"\ud800.py": "x = 1\n"})
    assert named.value.code == "bundle_invalid"


def test_patch_is_the_candidate_diff_and_paths_cannot_escape() -> None:
    parsed = parse_workspace(json.dumps(_workspace()).encode())
    patch = unified_patch(parsed["base_files"], parsed["files"])
    assert "return 0" in patch
    assert "return 42" in patch
    assert patch.startswith("--- ")
    escaped = _workspace()
    escaped["files"] = {"../app.py": "x"}
    with pytest.raises(WorkspaceError) as rejected:
        parse_workspace(json.dumps(escaped).encode())
    assert rejected.value.code == "path_rejected"


def _sandbox(passed: int, *, requests: int, edits: int = 0, patch: str = "") -> dict:
    return {
        "exit_code": 0,
        "result": {
            "passed": passed,
            "failed": 0 if passed else 1,
            "model_requests": requests,
            "edit_calls": edits,
            "patch": patch,
            "credential_present": False,
            "network_errno": 1,
        },
    }


def test_published_patch_comes_from_agent_edits_and_a_missed_call_cannot_finish() -> None:
    parsed = parse_workspace(json.dumps(_workspace()).encode())
    agent_patch = "--- a/app.py\n+++ b/app.py\n@@\n-return 0\n+return 7\n"
    edited = _sandbox(1, requests=2, edits=1, patch=agent_patch)
    assert _patch_source(edited) == "agent_edit"
    assert _published_patch(parsed, edited) == agent_patch.encode()
    untouched = _sandbox(1, requests=1)
    assert _patch_source(untouched) == "candidate_files"
    assert b"return 42" in _published_patch(parsed, untouched)
    assert _coding_terminal(untouched, upstream_error=None, measured=(8, 4), usage_missing=False) == ("FINISHED", None)
    blank = _sandbox(1, requests=0)
    assert _coding_terminal(blank, upstream_error=None, measured=None, usage_missing=False) == ("FAILED", "model_not_called")
    assert _coding_terminal(blank, upstream_error=None, measured=(8, 4), usage_missing=False) == ("FINISHED", None)
    assert _coding_terminal(untouched, upstream_error="upstream_http_400", measured=(8, 4), usage_missing=False) == (
        "FAILED",
        "upstream_http_400",
    )
    assert _coding_terminal(untouched, upstream_error=None, measured=None, usage_missing=True) == ("FAILED", "usage_missing")
    assert _coding_terminal(_sandbox(0, requests=1), upstream_error=None, measured=(8, 4), usage_missing=False) == (
        "FAILED",
        "test_failed",
    )


def test_coding_job_log_has_no_patch_text(caplog) -> None:
    caplog.set_level(logging.INFO, logger="aether.worker.coding")
    _log_job(
        job_id="job-1",
        fencing=2,
        state="FAILED",
        error="model_not_called",
        result={"model_requests": 0, "edit_calls": 0, "patch": "--- return 42 secret-body"},
        measured=None,
        patch_source="candidate_files",
    )
    assert "job-1" in caplog.text
    assert "model_not_called" in caplog.text
    assert "candidate_files" in caplog.text
    assert "return 42" not in caplog.text
    assert "secret-body" not in caplog.text


def test_coding_container_is_offline_read_only_and_limited(tmp_path) -> None:
    args = container_arguments(tmp_path, "python:3.12-alpine")
    assert args[0] == "run"
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "--pids-limit" in args and args[args.index("--pids-limit") + 1] == "64"
    assert "--memory" in args
    assert "--cpus" in args
    joined = " ".join(args)
    assert "docker.sock" not in joined
    assert "AETHER_LIVE_MODEL_KEY" not in joined
    assert "provider" not in joined.lower()
    assert str(CODING_MEMORY_BYTES) in args
    proxy = tmp_path / "aether-model-proof"
    proxy.mkdir()
    mounted = container_arguments(tmp_path, "python:3.12-alpine", proxy_dir=proxy, job_token="job-token-unit")
    mounted_text = " ".join(mounted)
    assert f"{proxy.resolve()}:/run/aether/proxy:rw" in mounted_text
    assert "AETHER_MODEL_SOCKET=/run/aether/proxy/model.sock" in mounted_text
    assert "AETHER_JOB_TOKEN=job-token-unit" in mounted_text
    assert "AETHER_MODEL_ALIAS=openai/coder" in mounted_text
    assert mounted[mounted.index("--network") + 1] == "none"
    assert "AETHER_LIVE_MODEL_KEY" not in mounted_text


def test_one_passing_family_is_finite_and_not_eligible() -> None:
    metrics = metrics_from_grades(
        [{"verdict": "PASS", "sentinel_visible": False, "credential_present": False}],
        baseline_passes=0,
        candidate_cost=0,
        baseline_cost=0,
    )
    assert metrics["families"] == 1
    assert metrics["grade_passes"] == 1
    assert math.isfinite(metrics["ci_lower"])
    assert math.isfinite(metrics["success_delta"])
    assert math.isfinite(metrics["cost_ratio"])
    assert metrics["cost_ratio"] == 1.0
    verdict = judge(metrics, synthetic=False)
    assert verdict["decision"] == "INSUFFICIENT_EVIDENCE"
    assert verdict["promoted"] is False


def test_divide_by_zero_cost_is_rejected_before_it_becomes_infinity() -> None:
    with pytest.raises(SkillEvalError) as rejected:
        metrics_from_grades(
            [{"verdict": "PASS", "sentinel_visible": False, "credential_present": False}],
            baseline_passes=0,
            candidate_cost=1,
            baseline_cost=0,
        )
    assert rejected.value.code == "non_finite"


def test_dataset_manifest_rejects_a_holdout_split_for_development() -> None:
    body = {
        "kind": "aether.skill_dataset",
        "split": "holdout",
        "synthetic": False,
        "contract": "合同",
        "cases": [{"family": "one", "suite_files": {"test_trusted.py": "def test_one():\n    assert True\n"}}],
    }
    parsed = parse_dataset(json.dumps(body).encode())
    assert parsed["split"] == "holdout"
    with pytest.raises(SkillEvalError) as isolated:
        require_development_split(parsed["split"])
    assert isolated.value.code == "dataset_isolated"
    candidate = parse_candidate(json.dumps({"kind": "aether.skill_candidate", "files": {"candidate.py": "x = 1\n"}}).encode())
    assert candidate["files"]["candidate.py"] == "x = 1\n"
    broken = dict(body)
    broken["cases"] = [{"family": "one", "suite_files": {"/tmp/x.py": "x"}}]
    with pytest.raises(DatasetError) as rejected:
        parse_dataset(json.dumps(broken).encode())
    assert rejected.value.code == "path_rejected"
    with pytest.raises(BundleError):
        parse_candidate(b"not-json")
