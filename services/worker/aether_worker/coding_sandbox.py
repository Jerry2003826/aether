"""Run one coding attempt in a networkless, read-only, resource-bounded container."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from aether_worker.runner import JOB_IMAGE, docker_available

CODING_RUNNER = r"""
import importlib.util, json, os, sys, time
from pathlib import Path

def copy_tree(src, dst):
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in {".git", "__pycache__"} for part in rel.parts):
            continue
        if path.is_symlink():
            raise RuntimeError("symlink")
        target = dst.joinpath(*rel.parts)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise RuntimeError("special")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())

def main():
    started = time.monotonic()
    dst = Path("/tmp/job")
    dst.mkdir(parents=True, exist_ok=True)
    copy_tree(Path("/work"), dst)
    sys.path.insert(0, str(dst))
    cases = []
    passed = 0
    failed = 0
    tests = sorted(path for path in dst.glob("test_*.py") if path.is_file() and not path.is_symlink())
    for path in tests:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            failed += 1
            cases.append({"name": path.stem, "verdict": "FAIL", "detail": "import"})
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        spec.loader.exec_module(module)
        for attr in sorted(dir(module)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(module, attr)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception as exc:
                failed += 1
                cases.append({"name": path.stem + "." + attr, "verdict": "FAIL", "detail": type(exc).__name__})
            else:
                passed += 1
                cases.append({"name": path.stem + "." + attr, "verdict": "PASS", "detail": ""})
    errno = None
    try:
        sock = __import__("socket").socket()
        sock.settimeout(1)
        sock.connect(("1.1.1.1", 443))
        errno = 0
    except OSError as exc:
        errno = exc.errno
    if not cases:
        failed += 1
        cases.append({"name": "suite", "verdict": "FAIL", "detail": "no_tests"})
    body = {
        "passed": passed,
        "failed": failed,
        "errors": 0,
        "cases": cases,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "network_errno": errno,
        "credential_present": any(any(part in name for part in ("LIVE_MODEL", "ADMIN_DATABASE", "PASSWORD", "SECRET")) for name in os.environ),
    }
    sys.stdout.write("RESULT " + json.dumps(body, separators=(",", ":")) + "\n")
    return 0 if failed == 0 and passed > 0 else 1

try:
    code = main()
except Exception as exc:
    sys.stdout.write("RESULT " + json.dumps({"passed": 0, "failed": 1, "errors": 1, "cases": [{"name": "runner", "verdict": "FAIL", "detail": type(exc).__name__}], "elapsed_seconds": 0, "network_errno": None, "credential_present": False}) + "\n")
    code = 2
raise SystemExit(code)
"""


class CodingSandboxError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def container_arguments(workspace: Path, image: str) -> list[str]:
    """Flags only. The caller supplies docker and does not inherit the worker environment."""
    return [
        "run",
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
        "--memory",
        "268435456",
        "--memory-swap",
        "268435456",
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,mode=1777",
        "--user",
        "65534:65534",
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "LANG=C.UTF-8",
        "-e",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "-v",
        f"{workspace.resolve()}:/work:ro",
        image,
        "python",
        "-I",
        "-c",
        CODING_RUNNER,
    ]


def run_coding_sandbox(workspace: Path, *, image: str = JOB_IMAGE, timeout_seconds: int = 45) -> dict[str, Any]:
    if not docker_available() or shutil.which("docker") is None:
        raise CodingSandboxError("sandbox_unavailable")
    name = f"aether-code-{uuid.uuid4().hex[:16]}"
    args = container_arguments(workspace, image)
    args.insert(1, name)
    args.insert(1, "--name")
    try:
        completed = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, check=False)
        raise CodingSandboxError("timeout") from exc
    result = _result_line(completed.stdout)
    if completed.returncode not in {0, 1} or result is None:
        raise CodingSandboxError("sandbox_failed")
    return {"exit_code": completed.returncode, "result": result, "timed_out": False}


def _result_line(stdout: str) -> dict[str, Any] | None:
    line = next((item for item in reversed(stdout.splitlines()) if item.startswith("RESULT ")), "")
    if not line:
        return None
    try:
        body = json.loads(line[len("RESULT ") :])
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    return body
