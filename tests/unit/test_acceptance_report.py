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


_SECTIONS = ("protocol", "coding", "research", "linux_isolation")
_CONFIG = "b" * 64


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Chain:
    """A git repo holding run evidence, an acceptance verdict and an owner approval.

    Evidence is produced at commit ``evidence_commit``; the verdict and approval
    are separate files committed afterwards. Only ``reports/f26_acceptance_result*.json``
    and ``reports/HANDOFF.md`` are evidence-only paths; STATUS.json is not.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        evidence: dict[str, object] | None = None,
        verdict: dict[str, object] | None = None,
        approval: dict[str, object] | None = None,
        with_verdict: bool = True,
        with_approval: bool = True,
        status: dict[str, object] | None = None,
    ) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        (self.repo / "reports").mkdir(parents=True)
        (self.repo / "src").mkdir()
        _git(self.repo, "init", "-q")
        self.status = self.repo / "STATUS.json"
        body = status or {
            "readiness": {"live_ready": "pending", "comparative_evidence": "not_run"},
            "f15": {"status": "done", "product_enabled": False},
            "f26": {"status": "done", "live_ready": False},
        }
        self.status.write_text(json.dumps(body), encoding="utf-8")
        (self.repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "reports" / "HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
        self.evidence_commit = _commit_all(self.repo, "code")
        short = self.evidence_commit[:7]
        evidence_body: dict[str, object] = {
            "status": "probed",
            "live_ready": False,
            "scripted_substitute": False,
            "git_commit": self.evidence_commit,
            **{name: {"status": "pass"} for name in _SECTIONS},
        }
        evidence_body.update(evidence or {})
        self.evidence = self.repo / "reports" / f"f26_acceptance_result_{short}.json"
        self.evidence.write_text(json.dumps(evidence_body), encoding="utf-8")
        self.evidence_sha = _sha(self.evidence)
        rel_evidence = f"reports/{self.evidence.name}"
        verdict_body: dict[str, object] = {
            "kind": "f26_acceptance_verdict",
            "evidence": rel_evidence,
            "evidence_sha256": self.evidence_sha,
            "evidence_git_commit": self.evidence_commit,
            "verdict": "passed",
            "checks": {name: "pass" for name in _SECTIONS},
        }
        verdict_body.update(verdict or {})
        self.verdict = self.repo / "reports" / f"f26_acceptance_result_{short}.verdict.json"
        if with_verdict:
            self.verdict.write_text(json.dumps(verdict_body), encoding="utf-8")
            approval_body: dict[str, object] = {
                "kind": "f26_owner_approval",
                "verdict": f"reports/{self.verdict.name}",
                "verdict_sha256": _sha(self.verdict),
                "evidence_sha256": self.evidence_sha,
                "approved": True,
                "owner": "Jerry2003826",
                "release_state": "approved",
            }
            approval_body.update(approval or {})
            if with_approval:
                approval_path = self.repo / "reports" / f"f26_acceptance_result_{short}.approval.json"
                approval_path.write_text(json.dumps(approval_body), encoding="utf-8")
        _commit_all(self.repo, "evidence")

    def commit(self, message: str = "change") -> None:
        _commit_all(self.repo, message)

    def report(self, *, commit: str | None = None) -> dict[str, object]:
        head = commit or _git(self.repo, "rev-parse", "HEAD")
        junit = self.tmp / "junit.xml"
        junit.write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            "<properties>"
            f'<property name="aether.git_commit" value="{head}"/>'
            '<property name="aether.environment" value="synthetic"/>'
            '<property name="aether.mode" value="live"/>'
            f'<property name="aether.config_sha256" value="{_CONFIG}"/>'
            f'<property name="aether.artifact.acceptance" value="{self.evidence_sha}"/>'
            '<property name="aether.required_cases" value="mod::ok"/>'
            "</properties>"
            '<testcase classname="mod" name="ok"/>'
            "</testsuite>",
            encoding="utf-8",
        )
        binding = {
            "git_commit": head,
            "environment": "synthetic",
            "mode": "live",
            "config_sha256": _CONFIG,
            "artifact_hashes": {"junit": _sha(junit), "acceptance": self.evidence_sha},
        }
        return build_report(junit, self.status, binding=binding)


def test_evidence_verdict_and_owner_approval_bound_to_current_commit_is_live_ready(tmp_path: Path) -> None:
    report = _Chain(tmp_path).report()
    assert report["passed"] is True
    assert report["binding_consistent"] is True
    assert report["live_ready"] is True
    assert report["live_ready_state"] == "passed"


def test_change_outside_evidence_whitelist_resets_live_ready_to_pending(tmp_path: Path) -> None:
    outside = {
        "status_json": ("STATUS.json", '{"readiness": {}, "f26": {"status": "done"}}'),
        "source": ("src/app.py", "VALUE = 2\n"),
        "other_report": ("reports/f27_result.json", "{}"),
        "nested_lookalike": ("reports/f26_acceptance_result_x/inner.json", "{}"),
        "root_lookalike": ("f26_acceptance_result_x.json", "{}"),
    }
    for name, (relative, text) in outside.items():
        chain = _Chain(tmp_path / name)
        assert chain.report()["live_ready_state"] == "passed", name
        target = chain.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        uncommitted = chain.report(commit=_git(chain.repo, "rev-parse", "HEAD"))
        assert uncommitted["live_ready"] is False, name
        assert uncommitted["live_ready_state"] == "pending", name
        chain.commit(name)
        report = chain.report()
        assert report["live_ready"] is False, name
        assert report["live_ready_state"] == "pending", name


def test_change_inside_evidence_whitelist_keeps_evidence(tmp_path: Path) -> None:
    chain = _Chain(tmp_path)
    (chain.repo / "reports" / "HANDOFF.md").write_text("# handoff\n\nupdated\n", encoding="utf-8")
    (chain.repo / "reports" / "f26_acceptance_result_old.json").write_text("{}", encoding="utf-8")
    chain.commit("record")
    report = chain.report()
    assert report["live_ready"] is True
    assert report["live_ready_state"] == "passed"


def test_hand_edited_status_live_ready_true_fails_acceptance(tmp_path: Path) -> None:
    claimed = {
        "readiness": {"live_ready": "passed", "comparative_evidence": "not_run"},
        "f15": {"status": "done", "product_enabled": False},
        "f26": {"status": "done", "live_ready": True, "acceptance_head": "x"},
    }
    self_certified = _Chain(tmp_path / "self_certified", evidence={"live_ready": True}, with_verdict=False)
    pointed = dict(claimed)
    pointed["f26"] = {
        "status": "done",
        "live_ready": True,
        "acceptance_head": self_certified.evidence_commit[:7],
        "acceptance_result": f"reports/{self_certified.evidence.name}",
    }
    self_certified.status.write_text(json.dumps(pointed), encoding="utf-8")
    report = self_certified.report(commit=self_certified.evidence_commit)
    assert report["live_ready"] is False
    assert report["live_ready_state"] == "pending"

    no_chain = _Chain(tmp_path / "no_chain", status=claimed, with_verdict=False)
    report = no_chain.report()
    assert report["passed"] is True
    assert report["live_ready"] is False
    assert report["live_ready_state"] == "pending"

    edited = _Chain(tmp_path / "edited")
    edited.status.write_text(json.dumps(claimed), encoding="utf-8")
    edited.commit("hand edit")
    report = edited.report()
    assert report["live_ready"] is False
    assert report["live_ready_state"] == "pending"


def test_chain_rejects_any_broken_link(tmp_path: Path) -> None:
    cases: dict[str, dict[str, object]] = {
        "no_approval": {"with_approval": False},
        "no_verdict": {"with_verdict": False},
        "evidence_not_probed": {"evidence": {"status": "failed"}},
        "evidence_scripted": {"evidence": {"scripted_substitute": True}},
        "evidence_section_failed": {"evidence": {"research": {"status": "fail"}}},
        "evidence_short_commit": {"evidence": {"git_commit": "abc1234"}},
        "verdict_wrong_evidence_hash": {"verdict": {"evidence_sha256": "e" * 64}},
        "verdict_wrong_commit": {"verdict": {"evidence_git_commit": "c" * 40}},
        "verdict_wrong_path": {"verdict": {"evidence": "reports/f26_acceptance_result.json"}},
        "verdict_not_passed": {"verdict": {"verdict": "failed"}},
        "verdict_checks_disagree": {"verdict": {"checks": {"protocol": "pass", "coding": "pass", "research": "pass"}}},
        "approval_wrong_verdict_hash": {"approval": {"verdict_sha256": "e" * 64}},
        "approval_wrong_evidence_hash": {"approval": {"evidence_sha256": "e" * 64}},
        "approval_not_approved": {"approval": {"approved": False}},
        "approval_release_pending": {"approval": {"release_state": "pending"}},
        "approval_no_owner": {"approval": {"owner": ""}},
    }
    for name, overrides in cases.items():
        report = _Chain(tmp_path / name, **overrides).report()  # type: ignore[arg-type]
        assert report["live_ready"] is False, name
        assert report["live_ready_state"] == "pending", name


def test_chain_still_checks_binding_commit_and_ancestry(tmp_path: Path) -> None:
    chain = _Chain(tmp_path)
    assert chain.report(commit="c" * 40)["live_ready"] is False
    before = _Chain(tmp_path / "before")
    assert before.report(commit=before.evidence_commit)["live_ready"] is False
