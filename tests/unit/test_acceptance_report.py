import hashlib
import json
import subprocess
import sys
from pathlib import Path

from aether_worker.acceptance_report import build_report

ROOT = Path(__file__).resolve().parents[2]
RECORDED_F26 = ROOT / "reports" / "f26_acceptance_result.json"
RECORDED_F26_SHA256 = "52ffd4b2da3a463ddea421db53098b4aaa441d0e7d87d3fc9f900af1d325db38"


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
    assert report["counts"]["executed"] == 2
    assert report["passed"] is False
    assert report["status"] == "fail"
    assert report["live_ready"] is False
    assert report["codegraph_enabled"] is False
    assert report["comparative_evidence"] == "not_run"
    assert "F26 live model" in report["not_run"]
    assert "f26" in report["incomplete"]


def test_live_ready_follows_status_and_leaves_hermes_and_codegraph_unrun(tmp_path: Path) -> None:
    """A STATUS claim cannot certify a suite that declares a test and contains none."""
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"></testsuite>\n',
        encoding="utf-8",
    )
    status = tmp_path / "STATUS.json"
    status.write_text(
        json.dumps(
            {
                "readiness": {"live_ready": "passed", "comparative_evidence": "not_run"},
                "f15": {"status": "done", "product_enabled": False},
                "f26": {"status": "done", "live_ready": True, "semantic_support": "not_judged", "cost_status": "unknown"},
            }
        ),
        encoding="utf-8",
    )
    report = build_report(junit, status)
    assert report["passed"] is False
    assert report["status"] == "blocked"
    assert report["live_ready"] is False
    assert "F26 live model" not in report["not_run"]
    assert report["not_run"] == ["Hermes comparison", "CodeGraph product enablement"]
    assert report["comparative_evidence"] == "not_run"
    assert report["codegraph_enabled"] is False


def test_verdicts_stay_distinct(tmp_path: Path) -> None:
    status = tmp_path / "STATUS.json"
    status.write_text("{}", encoding="utf-8")
    samples = {
        "pass": '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="m" name="ok"/></testsuite>',
        "fail": '<testsuite tests="1" failures="1" errors="0" skipped="0"><testcase classname="m" name="bad"><failure/></testcase></testsuite>',
        "not_run": '<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase classname="m" name="skip"><skipped/></testcase></testsuite>',
        "blocked": '<testsuite tests="2" failures="0" errors="0" skipped="0"><testcase classname="m" name="only"/></testsuite>',
    }
    seen: dict[str, bool] = {}
    for expected, xml in samples.items():
        path = tmp_path / f"{expected}.xml"
        path.write_text(xml, encoding="utf-8")
        report = build_report(path, status)
        assert report["status"] == expected
        seen[expected] = report["passed"]
    assert set(seen) == {"pass", "fail", "not_run", "blocked"}
    assert seen["pass"] is True
    assert seen["fail"] is False
    assert seen["not_run"] is False
    assert seen["blocked"] is False


def test_empty_and_all_skipped_and_later_suite_cannot_pass(tmp_path: Path) -> None:
    status = tmp_path / "STATUS.json"
    status.write_text(json.dumps({"readiness": {"live_ready": "passed"}, "f26": {"status": "done", "live_ready": True}}), encoding="utf-8")
    empty = tmp_path / "empty.xml"
    empty.write_text('<testsuite tests="0" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    empty_report = build_report(empty, status)
    assert empty_report["passed"] is False
    assert empty_report["status"] == "not_run"
    assert empty_report["counts"]["executed"] == 0
    assert empty_report["live_ready"] is False

    skipped = tmp_path / "skipped.xml"
    skipped.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase name="skip"><skipped/></testcase></testsuite>',
        encoding="utf-8",
    )
    skipped_report = build_report(skipped, status)
    assert skipped_report["passed"] is False
    assert skipped_report["status"] == "not_run"
    assert skipped_report["live_ready"] is False

    later = tmp_path / "later.xml"
    later.write_text(
        "<testsuites>"
        '<testsuite tests="1" failures="0" errors="0"><testcase name="good"/></testsuite>'
        '<testsuite tests="1" failures="1" errors="0"><testcase name="bad"><failure/></testcase></testsuite>'
        "</testsuites>",
        encoding="utf-8",
    )
    later_report = build_report(later, status)
    assert later_report["passed"] is False
    assert later_report["status"] == "fail"
    assert later_report["counts"]["tests"] == 2
    assert later_report["counts"]["failures"] == 1
    assert later_report["live_ready"] is False


def test_current_failure_does_not_inherit_stale_live_ready(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite tests="1" failures="1" errors="0"><testcase name="bad"><failure/></testcase></testsuite>', encoding="utf-8")
    status = tmp_path / "STATUS.json"
    status.write_text(
        json.dumps({"readiness": {"live_ready": "passed"}, "f26": {"status": "done", "live_ready": True}}),
        encoding="utf-8",
    )
    report = build_report(junit, status)
    assert report["passed"] is False
    assert report["live_ready"] is False


def test_required_case_must_be_executed(tmp_path: Path) -> None:
    status = tmp_path / "STATUS.json"
    status.write_text("{}", encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<properties><property name="aether.required_cases" value="mod::needed,mod::ran"/></properties>'
        '<testcase classname="mod" name="ran"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    report = build_report(junit, status)
    assert report["passed"] is False
    assert report["status"] == "fail"
    assert report["missing_required"] == ["mod::needed"]
    assert report["live_ready"] is False


def test_green_junit_without_current_binding_is_not_live_ready(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="mod" name="ok"/></testsuite>',
        encoding="utf-8",
    )
    status = tmp_path / "STATUS.json"
    status.write_text(
        json.dumps({"readiness": {"live_ready": "passed"}, "f26": {"status": "done", "live_ready": True}}),
        encoding="utf-8",
    )
    report = build_report(junit, status)
    assert report["passed"] is True
    assert report["status"] == "pass"
    assert report["counts"]["executed"] == 1
    assert report["live_ready"] is False
    assert report["binding_consistent"] is False


def _binding_fixture(tmp_path: Path, *, result_live: bool, commit: str = "a" * 40) -> tuple[Path, Path, dict[str, object]]:
    config = "b" * 64
    junit = tmp_path / "junit.xml"
    junit.write_text(
        "<testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\">"
        "<properties>"
        f'<property name="aether.git_commit" value="{commit}"/>'
        '<property name="aether.environment" value="synthetic"/>'
        '<property name="aether.mode" value="live"/>'
        f'<property name="aether.config_sha256" value="{config}"/>'
        '<property name="aether.artifact.acceptance" value="PLACEHOLDER"/>'
        '<property name="aether.required_cases" value="mod::ok"/>'
        "</properties>"
        '<testcase classname="mod" name="ok"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    result = tmp_path / "acceptance.json"
    result.write_text(json.dumps({"live_ready": result_live, "git_commit": commit}), encoding="utf-8")
    acceptance_hash = hashlib.sha256(result.read_bytes()).hexdigest()
    text = junit.read_text(encoding="utf-8").replace("PLACEHOLDER", acceptance_hash)
    junit.write_text(text, encoding="utf-8")
    junit_hash = hashlib.sha256(junit.read_bytes()).hexdigest()
    status = tmp_path / "STATUS.json"
    status.write_text(
        json.dumps(
            {
                "readiness": {"live_ready": "passed", "comparative_evidence": "not_run"},
                "f15": {"status": "done", "product_enabled": False},
                "f26": {
                    "status": "done",
                    "live_ready": True,
                    "acceptance_head": commit[:12],
                    "acceptance_result": "acceptance.json",
                },
            }
        ),
        encoding="utf-8",
    )
    binding = {
        "git_commit": commit,
        "environment": "synthetic",
        "mode": "live",
        "config_sha256": config,
        "artifact_hashes": {"junit": junit_hash, "acceptance": acceptance_hash},
    }
    return junit, status, binding


def test_live_ready_requires_matching_current_binding(tmp_path: Path) -> None:
    junit, status, binding = _binding_fixture(tmp_path, result_live=True)
    report = build_report(junit, status, binding=binding)
    assert report["passed"] is True
    assert report["status"] == "pass"
    assert report["missing_required"] == []
    assert report["binding_consistent"] is True
    assert report["live_ready"] is True
    assert report["not_run"] == ["Hermes comparison", "CodeGraph product enablement"]

    stale = dict(binding)
    stale["git_commit"] = "c" * 40
    mismatched = build_report(junit, status, binding=stale)
    assert mismatched["live_ready"] is False
    assert mismatched["binding_consistent"] is False


def test_result_file_live_ready_false_blocks_even_when_status_says_passed(tmp_path: Path) -> None:
    junit, status, binding = _binding_fixture(tmp_path, result_live=False)
    report = build_report(junit, status, binding=binding)
    assert report["passed"] is True
    assert report["live_ready"] is False


def test_recorded_f26_file_stays_historical_and_status_live_is_pending() -> None:
    digest = hashlib.sha256(RECORDED_F26.read_bytes()).hexdigest()
    assert digest == RECORDED_F26_SHA256
    recorded = json.loads(RECORDED_F26.read_text(encoding="utf-8"))
    assert recorded["live_ready"] is False
    assert recorded["git_commit"] == "d694f89d8a841f42bffba4457ad42336d447f98b"
    status = json.loads((ROOT / "STATUS.json").read_text(encoding="utf-8"))
    assert status["readiness"]["live_ready"] == "pending"
    assert status["f26"]["live_ready"] is False
    assert status["f26"]["acceptance_head"] == "d694f89"
    assert status["f26"]["acceptance_result"] == "reports/f26_acceptance_result.json"


def test_repository_status_cannot_promote_a_failed_junit(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="1" errors="0" skipped="0"><testcase name="bad"><failure/></testcase></testsuite>',
        encoding="utf-8",
    )
    report = build_report(junit, ROOT / "STATUS.json")
    assert report["passed"] is False
    assert report["live_ready"] is False
