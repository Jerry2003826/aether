"""Independent grading. Protocol assertions stay outside the candidate process."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.grader_protocol import extract_protocol, probes_document, values_match
from aether_worker.grader_runner import GRADER_PROGRAM as _GRADER_PROGRAM
from aether_worker.container_runtime import build_from_dockerfile
from aether_worker.runner import JOB_IMAGE, _docker
from aether_worker.sandbox_image import grader_dockerfile
from aether_worker.snapshot import SnapshotRejected, copy_tree, ensure_real_directory, read_tree

GRADER_IMAGE = "aether-grader:local"
_GRADER_ID: str | None = None
_DOCKER_IMAGE = "image"
_DOCKER_INSPECT = "inspect"
_SKIP_NAMES = {"conftest.py", "pytest.ini", "conftest.pyc"}
_SHADOW_ROOTS = frozenset({"pytest.py", "pytest", "_pytest", "pluggy.py", "pluggy", "sitecustomize.py", "usercustomize.py"})


class VerificationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _export_skip(parts: tuple[str, ...], is_dir: bool) -> bool:
    if is_dir:
        return parts[-1] in {".git", "__pycache__"}
    return ".git" in parts or "__pycache__" in parts


def export_candidate(source: Path) -> dict[str, Any]:
    try:
        files = read_tree(source, skip=_export_skip)
    except SnapshotRejected as exc:
        raise VerificationError(exc.code) from exc
    manifest = [{"path": path, "sha256": _sha256_bytes(data)} for path, data in files]
    digest = hashlib.sha256("\n".join(f"{item['sha256']} {item['path']}" for item in manifest).encode()).hexdigest()
    return {"files": manifest, "candidate_sha256": digest}


def text_sha256(value: str) -> str:
    return _sha256_bytes(value.encode())


def _prepare_skip(parts: tuple[str, ...], is_dir: bool) -> bool:
    if is_dir:
        return parts[-1] in {".git", "__pycache__", "conftest.py"}
    return ".git" in parts or "__pycache__" in parts or parts[-1] == "conftest.py"


def prepare_workspace(source: Path, destination: Path) -> None:
    try:
        copy_tree(source, destination, skip=_prepare_skip, file_mode=None)
    except SnapshotRejected as exc:
        raise VerificationError(exc.code) from exc


def _is_candidate_test(path: Path) -> bool:
    name = path.name
    return name in _SKIP_NAMES or name.startswith("test_") or name.endswith("_test.py")


def _shadowed_import(candidate: Path) -> str | None:
    for name in sorted(_SHADOW_ROOTS):
        if (candidate / name).exists():
            return name
    return None


def _stage_candidate_skip(parts: tuple[str, ...], is_dir: bool) -> bool:
    if parts[0] in _SHADOW_ROOTS:
        return True
    if is_dir:
        return False
    return _is_candidate_test(Path(parts[-1]))


def _stage_suite_skip(parts: tuple[str, ...], is_dir: bool) -> bool:
    if is_dir and parts[-1] != "conftest.py":
        return False
    return parts[-1] == "conftest.py"


def _stage(candidate: Path, suite: Path, destination: Path) -> None:
    try:
        ensure_real_directory(destination, replace=True)
        copy_tree(candidate, destination / "src", skip=_stage_candidate_skip, file_mode=0o644)
        copy_tree(suite, destination / "suite", skip=_stage_suite_skip, file_mode=0o644)
    except SnapshotRejected as exc:
        raise VerificationError(exc.code) from exc


def _ensure_grader_image() -> str | None:
    """Build the hash-locked grader and return its image id, never a mutable tag."""
    global _GRADER_ID
    if _GRADER_ID and _docker([_DOCKER_IMAGE, _DOCKER_INSPECT, _GRADER_ID]).returncode == 0:
        return _GRADER_ID
    dockerfile = grader_dockerfile()
    stamp = hashlib.sha256(dockerfile.encode()).hexdigest()
    found = _image_by_stamp(stamp)
    if found is None:
        if _docker([_DOCKER_IMAGE, _DOCKER_INSPECT, JOB_IMAGE]).returncode != 0:
            pulled = _docker(["pull", JOB_IMAGE])
            if pulled.returncode != 0:
                return None
        built = build_from_dockerfile(dockerfile, tag=GRADER_IMAGE, labels={"aether.grader_dockerfile": stamp})
        if built.returncode != 0:
            return None
        found = _image_by_stamp(stamp)
    if found is None or not found.startswith("sha256:"):
        return None
    _GRADER_ID = found
    return found


def _image_by_stamp(stamp: str) -> str | None:
    listed = _docker(["images", "--filter", f"label=aether.grader_dockerfile={stamp}", "--format", "{{.ID}}"])
    if listed.returncode != 0 or not listed.stdout.strip():
        return None
    inspected = _docker([_DOCKER_IMAGE, _DOCKER_INSPECT, "--format", "{{.Id}}", listed.stdout.strip().splitlines()[0]])
    if inspected.returncode != 0:
        return None
    image_id = inspected.stdout.strip()
    return image_id or None


def run_grader_container(
    workspace: Path,
    *,
    timeout_seconds: float | None,
    host_sentinel: str = "/aether-host-sentinel",
) -> subprocess.CompletedProcess[str]:
    """Run the grader. A deadline names the container so a timeout can remove it."""
    name = None if timeout_seconds is None else f"aether-grade-{uuid.uuid4().hex[:20]}"
    args = ["run"]
    if name is not None:
        args.extend(["--name", name])
    args.extend(
        [
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
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
            f"AETHER_HOST_SENTINEL={host_sentinel}",
            "-v",
            f"{workspace}:/work:ro",
            GRADER_IMAGE,
            "python",
            "-I",
            "-c",
            _GRADER_PROGRAM,
        ]
    )
    try:
        return _docker(args, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if name is not None:
            _docker(["rm", "-f", name])
        raise VerificationError("deadline_exceeded") from exc


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
    timeout_seconds: float | None = None,
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
    grader_image = _ensure_grader_image()
    if grader_image is None:
        return _result("FAIL", "评分容器不可用", suite_sha256, contract_sha256, observed_candidate)
    protocol = extract_protocol(candidate, suite)
    with tempfile.TemporaryDirectory(prefix="aether-grade-") as temporary:
        workspace = Path(temporary) / "work"
        _stage(candidate, suite, workspace)
        if protocol is not None:
            _publish_protocol(workspace, protocol)
        sentinel = str(host_sentinel) if host_sentinel is not None else "/aether-host-sentinel"
        completed = run_grader_container(workspace, timeout_seconds=timeout_seconds, host_sentinel=sentinel)
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
