"""Build the delivery report from a JUnit file and STATUS.json."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _counts(junit_path: Path) -> dict[str, int]:
    root = ET.parse(junit_path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise ValueError("junit_missing_suite")
    return {
        "tests": int(suite.attrib.get("tests", "0")),
        "failures": int(suite.attrib.get("failures", "0")),
        "errors": int(suite.attrib.get("errors", "0")),
        "skipped": int(suite.attrib.get("skipped", "0")),
    }


def build_report(junit_path: Path, status_path: Path) -> dict[str, Any]:
    counts = _counts(junit_path)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    incomplete = [key for key, value in status.items() if isinstance(value, dict) and value.get("status") != "done"]
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    f26 = status.get("f26") if isinstance(status.get("f26"), dict) else {}
    not_run = []
    if f26.get("status") != "done":
        not_run.append("F26 live model")
    if readiness.get("comparative_evidence") != "passed":
        not_run.append("Hermes comparison")
    if not bool(status.get("f15", {}).get("product_enabled")):
        not_run.append("CodeGraph product enablement")
    return {
        "source_junit": str(junit_path),
        "counts": counts,
        "passed": counts["failures"] == 0 and counts["errors"] == 0,
        "kernel_engineering": "see_status",
        "product_engineering": "incomplete" if incomplete else "status_done",
        "live_ready": readiness.get("live_ready") == "passed",
        "codegraph_enabled": bool(status.get("f15", {}).get("product_enabled")),
        "comparative_evidence": readiness.get("comparative_evidence", "not_run"),
        "incomplete": incomplete,
        "not_run": not_run,
    }
