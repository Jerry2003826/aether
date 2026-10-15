"""Independent grading. The bound suite runs in the sandbox, not the candidate's tests."""

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
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "reason": reason,
        "suite_sha256": suite_sha256,
        "contract_sha256": contract_sha256,
        "candidate_sha256": candidate_sha256,
        "sentinel_visible": sentinel_visible,
        "credential_present": credential_present,
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
    with tempfile.TemporaryDirectory(prefix="aether-grade-") as temporary:
        workspace = Path(temporary) / "work"
        _stage(candidate, suite, workspace)
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
                "/run/aether:rw,nosuid,nodev,mode=0700",
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
    if parsed["exit_code"] != 0 or not parsed["passed"]:
        return _result(
            "FAIL",
            "绑定套件未通过",
            suite_sha256,
            contract_sha256,
            observed_candidate,
            sentinel_visible=False,
            credential_present=False,
        )
    return _result("PASS", "绑定套件通过且哈希一致", suite_sha256, contract_sha256, observed_candidate)


_GRADER_PROGRAM = """
import json, os, subprocess, sys
from pathlib import Path
sentinel = Path(os.environ.get("AETHER_HOST_SENTINEL", "/aether-host-sentinel"))
keys = ("AETHER_PROVIDER_KEY", "AETHER_ADMIN_DATABASE_URL")
isolation = {"sentinel_visible": sentinel.exists(), "credential_present": any(key in os.environ for key in keys)}
sys.stdout.write("ISOLATION " + json.dumps(isolation) + "\\n")
sys.stdout.flush()
read_fd, write_fd = os.pipe()
os.set_inheritable(write_fd, True)
child = (
    "import json, os, sys\\n"
    "import pytest\\n"
    "sys.path.append('/work/src')\\n"
    f"fd = {write_fd}\\n"
    "class Report:\\n"
    "    passed_count = 0\\n"
    "    exit_code = 1\\n"
    "    def pytest_sessionfinish(self, session, exitstatus):\\n"
    "        reporter = session.config.pluginmanager.get_plugin('terminalreporter')\\n"
    "        stats = getattr(reporter, 'stats', {}) if reporter else {}\\n"
    "        Report.passed_count = len(stats.get('passed', []))\\n"
    "        Report.exit_code = int(exitstatus)\\n"
    "code = int(pytest.main(['-q', '--tb=no', '--noconftest', '-p', 'no:cacheprovider', '--rootdir', '/work/suite', '/work/suite'], plugins=[Report()]))\\n"
    "os.write(fd, json.dumps({'exit_code': Report.exit_code, 'passed_count': Report.passed_count}).encode())\\n"
    "os.close(fd)\\n"
    "os._exit(code if code >= 0 else 1)\\n"
)
completed = subprocess.run(
    [sys.executable, "-I", "-c", child],
    cwd="/work/suite",
    pass_fds=(write_fd,),
    env={
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/run/aether",
        "TMPDIR": "/run/aether",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    },
    capture_output=True,
    text=True,
)
os.close(write_fd)
raw = os.read(read_fd, 65536)
os.close(read_fd)
try:
    report = json.loads(raw.decode().strip().splitlines()[-1])
except (ValueError, IndexError):
    report = {"exit_code": 1, "passed_count": 0}
ok = int(report.get("exit_code", 1)) == 0 and int(report.get("passed_count", 0)) >= 1
sys.stdout.write("GRADE_OK:" + ("1" if ok else "0") + "\\n")
sys.stdout.write("GRADE_EXIT:" + str(int(report.get("exit_code", 1))) + "\\n")
del completed
"""


def _parse_grader(stdout: str) -> dict[str, Any]:
    lines = stdout.splitlines()
    isolation = {"sentinel_visible": True, "credential_present": True}
    if lines and lines[0].startswith("ISOLATION "):
        isolation = json.loads(lines[0].removeprefix("ISOLATION "))
    exit_code = 1
    passed = False
    for line in lines:
        if line.startswith("GRADE_EXIT:"):
            exit_code = int(line.split(":", 1)[1])
        elif line.startswith("GRADE_OK:"):
            passed = line.split(":", 1)[1].strip() == "1"
    return {
        "sentinel_visible": bool(isolation.get("sentinel_visible")),
        "credential_present": bool(isolation.get("credential_present")),
        "exit_code": exit_code,
        "passed": passed and exit_code == 0,
    }


def accept_actor_verdict(actor_verdict: str, graded: dict[str, Any]) -> dict[str, Any]:
    if actor_verdict != graded["verdict"]:
        graded = dict(graded)
        graded["reason"] = f"{graded['reason']}；已忽略 actor 的 {actor_verdict}"
    return graded


def record_grade(database: Database, *, owner_id: str, project_id: str, task_id: str, contract_id: str, artifact_id: str, report_artifact_id: str, graded: dict[str, Any]) -> str:
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
