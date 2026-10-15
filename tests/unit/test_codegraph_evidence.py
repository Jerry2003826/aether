import ast
import json
import subprocess
from pathlib import Path

from aether_worker.codegraph_evidence import CALL, ast_calls, ast_imports, edge_agrees, export_git_snapshot

ROOT = Path(__file__).resolve().parents[2]


def test_edge_agreement_rejects_a_caller_the_ast_does_not_have() -> None:
    calls = {("a.py", "build_report"): {("a.py", "_counts")}}
    callers = [
        {"name": "build_report", "kind": "function", "relative_path": "a.py", "relationships": ["calls"]},
        {"name": "other", "kind": "function", "relative_path": "b.py", "relationships": ["calls"]},
    ]
    callees = [{"name": "_counts", "kind": "function", "relative_path": "a.py", "relationships": ["calls"]}]
    report = edge_agrees(calls, "a.py", "build_report", "a.py", "_counts", callers, callees)
    assert report["caller_hit"] is True
    assert report["callee_hit"] is True
    assert report["extra_callers"] == ["b.py:other"]
    assert report["ok"] is False


def test_ast_sees_the_acceptance_report_call_and_the_live_harness_import() -> None:
    calls = ast_calls(ROOT)
    assert (CALL["callee_file"], CALL["callee"]) in calls[(CALL["caller_file"], CALL["caller"])]
    imports = ast_imports(ROOT, "services/worker/aether_worker/f26_acceptance.py")
    assert ("aether_worker.live_harness", "redact_secret") in imports


def test_export_skips_env_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    subprocess.run(["git", "add", "keep.py", ".env"], cwd=tmp_path, check=True, capture_output=True)
    destination = tmp_path / "out"
    destination.mkdir()
    copied = export_git_snapshot(tmp_path, destination)
    assert copied == 1
    assert (destination / "keep.py").is_file()
    assert not (destination / ".env").exists()
    parsed = ast.parse((destination / "keep.py").read_text(encoding="utf-8"))
    assert isinstance(parsed.body[0], ast.FunctionDef)


def test_recorded_evidence_is_the_only_way_to_mark_codegraph_enabled() -> None:
    status = json.loads((ROOT / "STATUS.json").read_text(encoding="utf-8"))
    evidence_path = ROOT / "reports" / "f15_product_result.json"
    enabled = status["readiness"]["codegraph_enabled"] == "passed" and status["f15"]["product_enabled"] is True
    if not evidence_path.is_file():
        assert enabled is False
        return
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    passed = evidence["status"] == "passed" and all(evidence["checks"].values()) and evidence["telemetry"] == "off"
    assert enabled is passed
