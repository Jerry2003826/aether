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


# Runs as root inside the sandbox and never imports candidate code. The bound
# suite runs in a child dropped to nobody, which cannot open this process's
# stdout through /proc. The verdict comes from the child's exit status and its
# JUnit report checked against the test names parsed from the trusted suite,
# not from anything the child chooses to print. Code running inside the child
# can still rewrite pytest itself; see docs/06_security.md.
_GRADER_PROGRAM = r"""
import ast, json, os, secrets, signal, stat, subprocess, sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

NOBODY = 65534
REPORT_LIMIT = 4 * 1024 * 1024
SUITE = Path("/work/suite")
CHILD_HOME = Path("/run/aether/child")


def required_tests(root):
    required = set()
    for path in sorted(root.rglob("*.py")):
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError, OSError):
            required.add((module, "<unparseable>"))
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                required.add((module, node.name))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        required.add((module + "." + node.name, item.name))
    return required


def read_report(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > REPORT_LIMIT:
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def grade():
    required = required_tests(SUITE)
    if not required:
        return {"ok": False, "reason": "suite_has_no_tests", "exit_code": None, "tests": 0}
    CHILD_HOME.mkdir(mode=0o700)
    os.chown(CHILD_HOME, NOBODY, NOBODY)
    report = CHILD_HOME / (secrets.token_hex(16) + ".xml")
    command = [
        sys.executable, "-I", "-m", "pytest", "-q", "--tb=no", "--noconftest", "-p", "no:cacheprovider",
        "-o", "pythonpath=/work/src", "--rootdir", str(SUITE), "--junitxml", str(report), str(SUITE),
    ]
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(CHILD_HOME),
        "TMPDIR": str(CHILD_HOME),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    try:
        completed = subprocess.run(
            command, cwd=str(SUITE), env=environment, user=NOBODY, group=NOBODY, extra_groups=[],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        exit_code = None
    # Nothing the candidate left running may touch the report after pytest returns.
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if exit_code != 0:
        return {"ok": False, "reason": "pytest_failed", "exit_code": exit_code, "tests": 0}
    data = read_report(report)
    if data is None:
        return {"ok": False, "reason": "report_missing", "exit_code": exit_code, "tests": 0}
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return {"ok": False, "reason": "report_unreadable", "exit_code": exit_code, "tests": 0}
    total = 0
    passed = set()
    for case in root.iter("testcase"):
        total += 1
        key = (case.get("classname", ""), case.get("name", "").split("[", 1)[0])
        if any(child.tag in {"failure", "error", "skipped"} for child in case):
            return {"ok": False, "reason": "test_not_passed", "exit_code": exit_code, "tests": total}
        passed.add(key)
    if total < 1 or not required <= passed:
        return {"ok": False, "reason": "required_test_missing", "exit_code": exit_code, "tests": total}
    return {"ok": True, "reason": "passed", "exit_code": exit_code, "tests": total}


sentinel = Path(os.environ.get("AETHER_HOST_SENTINEL", "/aether-host-sentinel"))
keys = ("AETHER_PROVIDER_KEY", "AETHER_ADMIN_DATABASE_URL")
isolation = {"sentinel_visible": sentinel.exists(), "credential_present": any(key in os.environ for key in keys)}
sys.stdout.write("ISOLATION " + json.dumps(isolation) + "\n")
sys.stdout.flush()
sys.stdout.write("GRADE " + json.dumps(grade()) + "\n")
sys.stdout.flush()
"""


def _parse_grader(stdout: str) -> dict[str, Any]:
    """Exactly an ISOLATION line then a GRADE line. Anything else is not a grade."""
    failed = {"sentinel_visible": True, "credential_present": True, "exit_code": 1, "passed": False}
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
    return {
        "sentinel_visible": isolation.get("sentinel_visible") is not False,
        "credential_present": isolation.get("credential_present") is not False,
        "exit_code": exit_code if isinstance(exit_code, int) else 1,
        "passed": graded.get("ok") is True and exit_code == 0,
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
