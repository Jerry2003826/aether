"""Independent grading. Protocol assertions stay outside the candidate process."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.grader_protocol import extract_protocol, probes_document, values_match
from aether_worker.grader_runner import GRADER_PROGRAM as _GRADER_PROGRAM
from aether_worker.runner import JOB_IMAGE, _docker

GRADER_IMAGE = "aether-grader:local"
_SKIP_NAMES = {"conftest.py", "pytest.ini", "conftest.pyc"}
_SHADOW_ROOTS = frozenset({"pytest.py", "pytest", "_pytest", "pluggy.py", "pluggy", "sitecustomize.py", "usercustomize.py"})


class VerificationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def export_candidate(source: Path) -> dict[str, Any]:
    files = sorted(path for path in source.rglob("*") if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts)
    manifest = [{"path": str(path.relative_to(source)), "sha256": _sha256_file(path)} for path in files]
    digest = hashlib.sha256("\n".join(f"{item['sha256']} {item['path']}" for item in manifest).encode()).hexdigest()
    return {"files": manifest, "candidate_sha256": digest}


def text_sha256(value: str) -> str:
    return _sha256_bytes(value.encode())


def prepare_workspace(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", "conftest.py"))


def _is_candidate_test(path: Path) -> bool:
    name = path.name
    return name in _SKIP_NAMES or name.startswith("test_") or name.endswith("_test.py")


def _shadowed_import(candidate: Path) -> str | None:
    for name in sorted(_SHADOW_ROOTS):
        if (candidate / name).exists():
            return name
    return None


def _stage(candidate: Path, suite: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    source_root = destination / "src"
    suite_root = destination / "suite"
    source_root.mkdir(parents=True)
    suite_root.mkdir()
    for path in candidate.rglob("*"):
        if not path.is_file() or _is_candidate_test(path):
            continue
        relative = path.relative_to(candidate)
        if relative.parts[0] in _SHADOW_ROOTS:
            continue
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for path in suite.rglob("*"):
        if not path.is_file() or path.name == "conftest.py":
            continue
        target = suite_root / path.relative_to(suite)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # The suite runs as nobody inside the sandbox and only needs to read.
    for path in [destination, *destination.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


def _ensure_grader_image() -> bool:
    if _docker(["image", "inspect", GRADER_IMAGE]).returncode == 0:
        return True
    if _docker(["image", "inspect", JOB_IMAGE]).returncode != 0:
        pulled = _docker(["pull", JOB_IMAGE])
        if pulled.returncode != 0:
            return False
    dockerfile = f"FROM {JOB_IMAGE}\nRUN pip install --no-cache-dir pytest==8.3.5\n"
    built = subprocess.run(
        ["docker", "build", "-t", GRADER_IMAGE, "-"],
        input=dockerfile,
        text=True,
        capture_output=True,
        check=False,
    )
    return built.returncode == 0


def _result(
    verdict: str,
    reason: str,
    suite_sha256: str,
    contract_sha256: str,
    candidate_sha256: str,
    *,
    sentinel_visible: bool = False,
    credential_present: bool = False,
    trust: str = "unverified",
    security_certified: bool = False,
    auto_promote: bool = False,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "reason": reason,
        "suite_sha256": suite_sha256,
        "contract_sha256": contract_sha256,
        "candidate_sha256": candidate_sha256,
        "sentinel_visible": sentinel_visible,
        "credential_present": credential_present,
        "trust": trust,
        "security_certified": security_certified,
        "auto_promote": auto_promote,
    }


def grade(
    candidate: Path,
    suite: Path,
    *,
    suite_sha256: str,
    contract_text: str,
    contract_sha256: str,
    candidate_sha256: str,
    host_sentinel: Path | None = None,
) -> dict[str, Any]:
    """Verify the bound suite and contract, then run only that suite inside the sandbox."""
    observed_suite = export_candidate(suite)["candidate_sha256"]
    observed_contract = text_sha256(contract_text)
    observed_candidate = export_candidate(candidate)["candidate_sha256"]
    if observed_suite != suite_sha256:
        return _result("FAIL", "套件哈希不一致", observed_suite, observed_contract, observed_candidate)
    if observed_contract != contract_sha256:
        return _result("FAIL", "合同哈希不一致", observed_suite, observed_contract, observed_candidate)
    if observed_candidate != candidate_sha256:
        return _result("FAIL", "候选哈希与绑定时不一致", observed_suite, observed_contract, observed_candidate)
    if _shadowed_import(candidate) is not None:
        return _result("FAIL", "候选包含会遮蔽评分器的模块", observed_suite, observed_contract, observed_candidate)
    if not _ensure_grader_image():
        return _result("FAIL", "评分容器不可用", suite_sha256, contract_sha256, observed_candidate)
    protocol = extract_protocol(candidate, suite)
    with tempfile.TemporaryDirectory(prefix="aether-grade-") as temporary:
        workspace = Path(temporary) / "work"
        _stage(candidate, suite, workspace)
        if protocol is not None:
            _publish_protocol(workspace, protocol)
        sentinel = str(host_sentinel) if host_sentinel is not None else "/aether-host-sentinel"
        completed = _docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--tmpfs",
                "/run/aether:rw,nosuid,nodev,mode=0711",
                "-e",
                "HOME=/run/aether",
                "-e",
                "TMPDIR=/run/aether",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "-e",
                f"AETHER_HOST_SENTINEL={sentinel}",
                "-v",
                f"{workspace}:/work:ro",
                GRADER_IMAGE,
                "python",
                "-I",
                "-c",
                _GRADER_PROGRAM,
            ]
        )
    if completed.returncode != 0 or not completed.stdout.startswith("ISOLATION "):
        return _result("FAIL", "评分容器没有完成", suite_sha256, contract_sha256, observed_candidate)
    parsed = _parse_grader(completed.stdout)
    if parsed["sentinel_visible"] or parsed["credential_present"]:
        return _result(
            "FAIL",
            "评分进程看到了宿主文件或密钥",
            suite_sha256,
            contract_sha256,
            observed_candidate,
            sentinel_visible=parsed["sentinel_visible"],
            credential_present=parsed["credential_present"],
        )
    return _verdict(protocol, parsed, suite_sha256, contract_sha256, observed_candidate)


def _publish_protocol(workspace: Path, protocol: tuple[Any, ...]) -> None:
    path = workspace / "protocol.json"
    path.write_text(json.dumps(probes_document(protocol), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)
    suite_root = workspace / "suite"
    suite_root.chmod(0o700)
    for child in suite_root.rglob("*"):
        child.chmod(0o600 if child.is_file() else 0o700)


def _verdict(
    protocol: tuple[Any, ...] | None,
    parsed: dict[str, Any],
    suite_sha256: str,
    contract_sha256: str,
    candidate_sha256: str,
) -> dict[str, Any]:
    if protocol is not None and parsed["trust"] == "isolated" and values_match(protocol, parsed["values"]):
        return _result(
            "PASS",
            "绑定套件通过且哈希一致",
            suite_sha256,
            contract_sha256,
            candidate_sha256,
            trust="isolated",
            security_certified=True,
            auto_promote=False,
        )
    if parsed["trust"] == "cooperative" and parsed["cooperative_passed"]:
        return _result(
            "FAIL",
            "白盒 pytest 只验证合作代码，不能作为安全认证或自动发布",
            suite_sha256,
            contract_sha256,
            candidate_sha256,
            trust="cooperative",
            security_certified=False,
            auto_promote=False,
        )
    trust = parsed["trust"] if parsed["trust"] in {"isolated", "cooperative"} else "unverified"
    return _result(
        "FAIL",
        "绑定套件未通过",
        suite_sha256,
        contract_sha256,
        candidate_sha256,
        trust=trust,
        security_certified=False,
        auto_promote=False,
    )


# An isolated pass can be recorded as a certification. It still does not promote.
_GRADE_CAN_PROMOTE = False


def allows_auto_promotion(graded: dict[str, Any]) -> bool:
    """Cooperative pytest cannot promote. A certified grade does not promote by itself."""
    if graded.get("trust") != "isolated" or graded.get("security_certified") is not True:
        return False
    if graded.get("verdict") != "PASS" or graded.get("auto_promote") is not True:
        return False
    return _GRADE_CAN_PROMOTE


def _recordable(graded: dict[str, Any]) -> bool:
    if graded.get("auto_promote") is True or allows_auto_promotion(graded):
        return False
    if graded.get("verdict") != "PASS":
        return True
    return graded.get("trust") == "isolated" and graded.get("security_certified") is True


def _parse_grader(stdout: str) -> dict[str, Any]:
    """Exactly an ISOLATION line then a GRADE line. Anything else is not a grade."""
    failed = {
        "sentinel_visible": True,
        "credential_present": True,
        "exit_code": 1,
        "trust": None,
        "values": None,
        "cooperative_passed": False,
    }
    lines = stdout.splitlines()
    if len(lines) != 2 or not lines[0].startswith("ISOLATION ") or not lines[1].startswith("GRADE "):
        return failed
    try:
        isolation = json.loads(lines[0].removeprefix("ISOLATION "))
        graded = json.loads(lines[1].removeprefix("GRADE "))
    except ValueError:
        return failed
    if not isinstance(isolation, dict) or not isinstance(graded, dict):
        return failed
    exit_code = graded.get("exit_code")
    trust = graded.get("trust")
    values = graded.get("values")
    return {
        "sentinel_visible": isolation.get("sentinel_visible") is not False,
        "credential_present": isolation.get("credential_present") is not False,
        "exit_code": exit_code if isinstance(exit_code, int) else 1,
        "trust": trust if trust in {"isolated", "cooperative"} else None,
        "values": values if isinstance(values, list) else None,
        "cooperative_passed": graded.get("cooperative_passed") is True,
    }


def accept_actor_verdict(actor_verdict: str, graded: dict[str, Any]) -> dict[str, Any]:
    if actor_verdict != graded["verdict"]:
        graded = dict(graded)
        graded["reason"] = f"{graded['reason']}；已忽略 actor 的 {actor_verdict}"
    return graded


def record_grade(database: Database, *, owner_id: str, project_id: str, task_id: str, contract_id: str, artifact_id: str, report_artifact_id: str, graded: dict[str, Any]) -> str:
    if not _recordable(graded):
        raise VerificationError("uncertified_pass")
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        identity = conn.execute(text("SELECT current_user")).scalar_one()
        if identity != "aether_verifier":
            raise VerificationError("verifier_required")
        row = conn.execute(
            text(
                "INSERT INTO verification_results (project_id, task_id, contract_id, artifact_id, criterion_id, verdict, "
                "artifact_sha256, contract_sha256, verifier_identity, report_artifact_id, reason, checked_at) "
                "VALUES (:project_id, :task_id, :contract_id, :artifact_id, 'candidate', :verdict, :artifact_sha256, "
                ":contract_sha256, :identity, :report_artifact_id, :reason, now()) RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "contract_id": contract_id,
                "artifact_id": artifact_id,
                "verdict": graded["verdict"],
                "artifact_sha256": graded["candidate_sha256"],
                "contract_sha256": graded["contract_sha256"],
                "identity": identity,
                "report_artifact_id": report_artifact_id,
                "reason": graded["reason"],
            },
        ).scalar_one()
    return str(row)
