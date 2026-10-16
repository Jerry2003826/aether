"""Coding-job workspace rules and skill metrics stay finite before any sandbox starts."""

from __future__ import annotations

import json
import math

import pytest

from aether_worker.bundle_files import BundleError, parse_file_map
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
