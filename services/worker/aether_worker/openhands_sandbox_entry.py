"""Sandbox entry. OpenHands LocalWorkspace runs the protocol. There is no importlib fallback."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"

from openhands.sdk.workspace.local import LocalWorkspace

WORK = Path(os.environ.get("AETHER_SANDBOX_WORK", "/work"))
PROTOCOL = Path(os.environ.get("AETHER_SANDBOX_PROTOCOL", "/opt/aether/coding_protocol.py"))
EXECUTOR = "openhands.sdk.LocalWorkspace"


def copy_tree(src: Path, dst: Path) -> None:
    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        if any(part in {".git", "__pycache__"} for part in relative.parts):
            continue
        if path.is_symlink():
            raise RuntimeError("symlink")
        target = dst.joinpath(*relative.parts)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise RuntimeError("special")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


def network_errno() -> int | None:
    sock = socket.socket()
    try:
        sock.settimeout(1)
        sock.connect(("1.1.1.1", 443))
    except OSError as exc:
        return exc.errno
    else:
        return 0
    finally:
        sock.close()


def credential_present() -> bool:
    markers = ("LIVE_MODEL", "ADMIN_DATABASE", "PASSWORD", "SECRET")
    return any(any(part in name for part in markers) for name in os.environ)


def emit(body: dict[str, object], code: int) -> None:
    sys.stdout.write("RESULT " + json.dumps(body, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    raise SystemExit(code)


def _failure(detail: str) -> dict[str, object]:
    return {
        "passed": 0,
        "failed": 1,
        "errors": 1,
        "cases": [{"name": "runner", "verdict": "FAIL", "detail": detail}],
        "elapsed_seconds": 0,
        "network_errno": network_errno(),
        "credential_present": credential_present(),
        "executor": EXECUTOR,
    }


def _protocol_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{") and line.endswith("}"):
            body = json.loads(line)
            if isinstance(body, dict):
                return body
    raise json.JSONDecodeError("protocol", stdout, 0)


def job_directory() -> Path:
    """Use the caller path, or a private directory from tempfile. A fixed public path is not created."""
    configured = os.environ.get("AETHER_SANDBOX_JOB")
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="aether-job-"))


def main() -> None:
    started = time.monotonic()
    try:
        job = job_directory()
        copy_tree(WORK, job)
        workspace = LocalWorkspace(working_dir=str(job))
        result = workspace.execute_command(
            f"{sys.executable} -I {PROTOCOL}",
            cwd=str(job),
            timeout=40,
        )
        payload = _protocol_payload(result.stdout or "")
    except SystemExit:
        raise
    except Exception as exc:
        emit(_failure(type(exc).__name__), 2)
    payload["elapsed_seconds"] = round(time.monotonic() - started, 6)
    payload["network_errno"] = network_errno()
    payload["credential_present"] = credential_present()
    payload["executor"] = EXECUTOR
    failed = int(payload.get("failed") or 0)
    passed = int(payload.get("passed") or 0)
    code = 0 if failed == 0 and passed > 0 and result.exit_code == 0 and not result.timeout_occurred else 1
    if result.timeout_occurred:
        payload["errors"] = int(payload.get("errors") or 0) + 1
    emit(payload, code)


if __name__ == "__main__":
    main()
