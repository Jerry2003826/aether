import json
import subprocess
import sys
from pathlib import Path

from aether_worker.acceptance_report import build_report


def test_report_counts_come_from_junit_and_live_stays_not_ready(tmp_path: Path) -> None:
    case = tmp_path / "test_sample.py"
    case.write_text("def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n", encoding="utf-8")
    junit = tmp_path / "junit.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", str(case), f"--junitxml={junit}"], check=False)
    status = tmp_path / "STATUS.json"
    status.write_text(json.dumps({"f15": {"status": "done", "product_enabled": False}, "f26": {"status": "in_progress"}}), encoding="utf-8")
    report = build_report(junit, status)
    assert report["counts"]["tests"] == 2
    assert report["counts"]["failures"] == 1
    assert report["passed"] is False
    assert report["live_ready"] is False
    assert report["codegraph_enabled"] is False
    assert report["comparative_evidence"] == "not_run"
    assert "F26 live model" in report["not_run"]
    assert "f26" in report["incomplete"]
