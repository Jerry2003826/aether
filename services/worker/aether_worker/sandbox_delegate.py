"""Worker-side coding sandbox calls. The runner process builds the docker argv."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from aether_worker.loopback import LoopbackError, post_json

_PROCESS_ROLE = "AETHER_PROCESS_ROLE"
_WORKER_ROLE = "worker"
_RUNNER_BASE = "AETHER_RUNNER_BASE"
_LOOPBACK_TOKEN = "AETHER_LOOPBACK_TOKEN"
_JOB = re.compile(r"[A-Za-z0-9-]{1,80}")
_NAME = re.compile(r"aether-code-[A-Za-z0-9]{1,32}|[0-9a-f]{12,64}")
_WORK = re.compile(r"/[A-Za-z0-9_./-]{1,240}")
_GRACE = re.compile(r"[0-9]{1,3}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}")
_CODING_PREFIX = "label=aether.coding="
_ROLE_FILTER = "label=aether.role=coding"


class SandboxDelegateError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def worker_delegates() -> bool:
    return os.environ.get(_PROCESS_ROLE) == _WORKER_ROLE


def accepted_job(value: object) -> str:
    return _exact(value, _JOB)


def accepted_name(value: object) -> str:
    return _exact(value, _NAME)


def accepted_grace(value: object) -> str:
    text = _exact(value, _GRACE)
    if int(text) > 120:
        raise SandboxDelegateError("rejected")
    return text


def accepted_token(value: object) -> str:
    return _exact(value, _TOKEN)


def accepted_proxy(value: object) -> Path:
    resolved = Path(str(value)).resolve()
    root = Path(tempfile.gettempdir()).resolve()
    if not resolved.is_dir() or not resolved.name.startswith("aether-model-"):
        raise SandboxDelegateError("rejected")
    if resolved != root and root not in resolved.parents:
        raise SandboxDelegateError("rejected")
    return Path(_exact(resolved, _WORK))


def accepted_workspace(value: object) -> Path:
    resolved = Path(str(value)).resolve()
    if not resolved.is_dir() or not resolved.name.startswith("aether-code-"):
        raise SandboxDelegateError("rejected")
    text = _exact(resolved, _WORK)
    return Path(text)


def remote_completed(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Translate one local docker argv into a fixed runner action."""
    fields = _fields(args)
    return _completed(_ask(str(fields.pop("op")), fields, timeout=timeout))


def remote_run(
    workspace: Path,
    job_id: str,
    *,
    proxy_dir: Path | None,
    job_token: str,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    proxy = accepted_proxy(proxy_dir)
    token = accepted_token(job_token)
    return _completed(
        _ask(
            "run",
            {"workspace": str(workspace.resolve()), "job_id": job_id, "proxy_dir": str(proxy), "job_token": token},
            timeout=timeout,
        )
    )


def remote_logs(name: str, limit: int, *, timeout: float | None = None) -> dict[str, str]:
    body = _ask("logs", {"name": name, "limit": limit}, timeout=timeout)
    return {"stdout": str(body.get("stdout") or ""), "stderr": str(body.get("stderr") or "")}


def _fields(args: list[str]) -> dict[str, str]:
    if args[:2] == ["ps", "-aq"] and len(args) == 6 and args[3].startswith(_CODING_PREFIX) and args[5] == _ROLE_FILTER:
        return {"op": "ps", "job_id": args[3][len(_CODING_PREFIX) :]}
    if args[:2] == ["stop", "-t"] and len(args) == 4:
        return {"op": "stop", "grace": args[2], "name": args[3]}
    if args[:2] == ["rm", "-f"] and len(args) == 3:
        return {"op": "rm", "name": args[2]}
    if args[:3] == ["inspect", "-f", "{{.State.Status}}"] and len(args) == 4:
        return {"op": "inspect_status", "name": args[3]}
    if args[:3] == ["inspect", "-f", "{{.State.ExitCode}}"] and len(args) == 4:
        return {"op": "inspect_exit", "name": args[3]}
    raise SandboxDelegateError("unmapped")


def _ask(op: str, fields: dict[str, object], *, timeout: float | None) -> dict[str, object]:
    base = os.environ.get(_RUNNER_BASE, "").rstrip("/")
    token = os.environ.get(_LOOPBACK_TOKEN, "")
    if not base or not token:
        raise SandboxDelegateError("runner_unavailable")
    try:
        body = post_json(f"{base}/v1/sandbox", token, {"op": op, **fields}, timeout=30 if timeout is None else timeout)
    except LoopbackError as exc:
        raise SandboxDelegateError(exc.code) from exc
    if body.get("error"):
        raise SandboxDelegateError(str(body["error"]))
    return body


def _completed(body: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["docker"],
        int(body.get("returncode") or 0),
        str(body.get("stdout") or ""),
        str(body.get("stderr") or ""),
    )


def _exact(value: object, pattern: re.Pattern[str]) -> str:
    matched = pattern.fullmatch(str(value))
    if matched is None:
        raise SandboxDelegateError("rejected")
    return matched.group(0)
