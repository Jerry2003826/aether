"""Independent grading. The actor's verdict and any JUnit file are not the result."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from aether_storage.db import Database

_ENV_ALLOW = ("PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH")


class VerificationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def export_candidate(source: Path) -> dict[str, Any]:
    files = sorted(path for path in source.rglob("*") if path.is_file() and ".git" not in path.parts)
    manifest = [{"path": str(path.relative_to(source)), "sha256": _sha256(path)} for path in files]
    digest = hashlib.sha256("\n".join(f"{item['sha256']} {item['path']}" for item in manifest).encode()).hexdigest()
    return {"files": manifest, "candidate_sha256": digest}


def prepare_workspace(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__"))


def _grader_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOW if key in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def grade(workspace: Path, *, suite_sha256: str, contract_sha256: str, candidate_sha256: str) -> dict[str, str]:
    """Run pytest in a clean workspace. A planted JUnit file cannot grant PASS."""
    observed = export_candidate(workspace)["candidate_sha256"]
    junit = workspace / "junit.xml"
    junit_claim = junit.read_text(encoding="utf-8") if junit.is_file() else ""
    if observed != candidate_sha256:
        return _result("FAIL", "候选哈希与绑定时不一致", suite_sha256, contract_sha256, observed)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", str(workspace)],
        cwd=workspace,
        env=_grader_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if "no tests ran" in output or "collected 0 items" in output:
        return _result("FAIL", "没有可执行的测试", suite_sha256, contract_sha256, observed)
    if completed.returncode != 0:
        return _result("FAIL", "pytest 未通过", suite_sha256, contract_sha256, observed)
    if "passed" not in completed.stdout:
        return _result("FAIL", "没有通过的测试", suite_sha256, contract_sha256, observed)
    if junit_claim and "failures=\"0\"" in junit_claim and completed.returncode != 0:
        return _result("FAIL", "忽略伪造的 JUnit", suite_sha256, contract_sha256, observed)
    return _result("PASS", "pytest 通过且哈希一致", suite_sha256, contract_sha256, observed)


def _result(verdict: str, reason: str, suite_sha256: str, contract_sha256: str, candidate_sha256: str) -> dict[str, str]:
    return {
        "verdict": verdict,
        "reason": reason,
        "suite_sha256": suite_sha256,
        "contract_sha256": contract_sha256,
        "candidate_sha256": candidate_sha256,
    }


def accept_actor_verdict(actor_verdict: str, graded: dict[str, str]) -> dict[str, str]:
    if actor_verdict != graded["verdict"]:
        graded = dict(graded)
        graded["reason"] = f"{graded['reason']}；已忽略 actor 的 {actor_verdict}"
    return graded


def record_grade(database: Database, *, owner_id: str, project_id: str, task_id: str, contract_id: str, artifact_id: str, report_artifact_id: str, graded: dict[str, str]) -> str:
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
