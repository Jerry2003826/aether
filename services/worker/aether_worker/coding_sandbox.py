"""Run one coding attempt in a networkless, read-only, resource-bounded container."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from aether_worker.container_runtime import container_logs
from aether_worker.openhands_image import launch_image
from aether_worker.runner import JOB_IMAGE, _docker, docker_available
from aether_worker.sandbox_delegate import (
    SandboxDelegateError,
    remote_completed,
    remote_logs,
    remote_run,
    worker_delegates,
)

CANCEL_GRACE_SECONDS = 10
LOG_TAIL_LIMIT = 4096
LOG_CAPTURE_LIMIT = 65536
POLL_SECONDS = 0.25
# 768 MiB. One agent-entry sample peaked at 283888 KiB, above the old 256 MiB cgroup.
CODING_MEMORY_BYTES = 805306368
CODING_SANDBOX_TIMEOUT_SECONDS = 120
_MODEL_SOCKET = "/run/aether/proxy/model.sock"


class CodingSandboxError(Exception):
    def __init__(self, code: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.payload = payload or {}


def container_name(job_id: str) -> str:
    token = "".join(character for character in job_id if character.isalnum())[:32]
    return f"aether-code-{token}"


def container_arguments(
    workspace: Path,
    image: str,
    *,
    job_id: str | None = None,
    proxy_dir: Path | None = None,
    job_token: str | None = None,
) -> list[str]:
    """Flags only. The caller supplies docker and does not inherit the worker environment."""
    labels: list[str] = []
    if job_id is not None:
        labels = ["--label", f"aether.coding={job_id}", "--label", "aether.role=coding"]
    proxy: list[str] = []
    if proxy_dir is not None and job_token:
        proxy = [
            "-e",
            f"AETHER_MODEL_SOCKET={_MODEL_SOCKET}",
            "-e",
            f"AETHER_JOB_TOKEN={job_token}",
            "-e",
            "AETHER_MODEL_ALIAS=openai/coder",
            "-v",
            f"{proxy_dir.resolve()}:/run/aether/proxy:rw",
        ]
    return [
        "run",
        *labels,
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
        str(CODING_MEMORY_BYTES),
        "--memory-swap",
        str(CODING_MEMORY_BYTES),
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,mode=1777",
        "--workdir",
        "/tmp",
        "--user",
        "65534:65534",
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
        "-e",
        "XDG_CACHE_HOME=/tmp",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "LANG=C.UTF-8",
        "-e",
        "PATH=/usr/local/bin:/usr/bin:/bin:/opt/openhands/.venv/bin",
        "-e",
        "OPENHANDS_SUPPRESS_BANNER=1",
        *proxy,
        "-v",
        f"{workspace.resolve()}:/work:ro",
        image,
        "/opt/openhands/.venv/bin/python",
        "-I",
        "/opt/aether/openhands_sandbox_entry.py",
    ]


def stop_arguments(job_id: str, *, grace_seconds: int = CANCEL_GRACE_SECONDS) -> list[list[str]]:
    """Stop, remove, then list. Confirmation is the empty list, not the stop command itself."""
    return [
        ["stop", "-t", str(grace_seconds), container_name(job_id)],
        ["rm", "-f", container_name(job_id)],
        ["ps", "-aq", "--filter", f"label=aether.coding={job_id}", "--filter", "label=aether.role=coding"],
    ]


def coding_container_ids(job_id: str) -> list[str] | None:
    if not docker_available():
        return None
    completed = _sandbox_docker(["ps", "-aq", "--filter", f"label=aether.coding={job_id}", "--filter", "label=aether.role=coding"])
    if completed.returncode != 0:
        return None
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def read_bounded_tail(stream: BinaryIO, limit: int) -> str:
    """Keep only the last `limit` bytes. The host does not retain the rest."""
    if limit < 1:
        return ""
    kept = bytearray()
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        kept.extend(chunk)
        if len(kept) > limit:
            del kept[:-limit]
    return kept.decode("utf-8", errors="replace")


def _bounded_logs(target: str, *, limit: int) -> dict[str, str]:
    """Both streams come back from the Docker SDK. An empty stream is b'', not a missing pipe."""
    if worker_delegates():
        try:
            return remote_logs(target, limit, timeout=60)
        except SandboxDelegateError as exc:
            raise CodingSandboxError("sandbox_unavailable") from exc
    if shutil.which("docker") is None:
        return {"stdout": "", "stderr": ""}
    stdout, stderr = container_logs(target, limit=limit)
    return {
        "stdout": read_bounded_tail(io.BytesIO(stdout), limit),
        "stderr": read_bounded_tail(io.BytesIO(stderr), limit),
    }


def container_log_tails(job_id: str) -> dict[str, str]:
    found = coding_container_ids(job_id)
    target = found[0] if found else container_name(job_id)
    logs = _bounded_logs(target, limit=LOG_CAPTURE_LIMIT)
    return {"stdout": logs["stdout"][-LOG_TAIL_LIMIT:], "stderr": logs["stderr"][-LOG_TAIL_LIMIT:]}


def confirm_coding_stop(job_id: str, *, grace_seconds: int = CANCEL_GRACE_SECONDS) -> bool:
    """True only when a second listing shows no container for this job."""
    if coding_container_ids(job_id) is None:
        return False
    for command in stop_arguments(job_id, grace_seconds=grace_seconds):
        if command[0] != "ps":
            _sandbox_docker(command)
    for item in coding_container_ids(job_id) or []:
        _sandbox_docker(["stop", "-t", str(grace_seconds), item])
        _sandbox_docker(["rm", "-f", item])
    remaining = coding_container_ids(job_id)
    return remaining == []


def release_coding_container(job_id: str) -> None:
    if not docker_available():
        return
    _sandbox_docker(["rm", "-f", container_name(job_id)])
    for item in coding_container_ids(job_id) or []:
        _sandbox_docker(["rm", "-f", item])


def run_coding_sandbox(
    workspace: Path,
    *,
    image: str = JOB_IMAGE,
    timeout_seconds: int = CODING_SANDBOX_TIMEOUT_SECONDS,
    job_id: str | None = None,
    proxy_dir: Path | None = None,
    job_token: str | None = None,
    stop_requested: Callable[[], bool] | None = None,
    deadline_reached: Callable[[], bool] | None = None,
    on_poll: Callable[[], None] | None = None,
    grace_seconds: int = CANCEL_GRACE_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> dict[str, Any]:
    if not docker_available() or (not worker_delegates() and shutil.which("docker") is None):
        raise CodingSandboxError("sandbox_unavailable")
    chosen = job_id or str(uuid.uuid4())
    _ensure_started(workspace, image, chosen, proxy_dir=proxy_dir, job_token=job_token)
    return _wait_until_stopped(
        chosen,
        timeout_seconds=timeout_seconds,
        stop_requested=stop_requested,
        deadline_reached=deadline_reached,
        on_poll=on_poll,
        grace_seconds=grace_seconds,
        poll_seconds=poll_seconds,
    )


def _ensure_started(
    workspace: Path,
    image: str,
    job_id: str,
    *,
    proxy_dir: Path | None,
    job_token: str | None,
) -> None:
    existing = coding_container_ids(job_id)
    if existing is None:
        raise CodingSandboxError("sandbox_unavailable")
    if existing:
        return
    if worker_delegates():
        if image != JOB_IMAGE:
            raise CodingSandboxError("sandbox_unavailable")
        try:
            launched = remote_run(workspace, job_id, proxy_dir=proxy_dir, job_token=job_token or "", timeout=180)
        except SandboxDelegateError as exc:
            raise CodingSandboxError("sandbox_unavailable") from exc
    else:
        try:
            runtime_image = launch_image(image, JOB_IMAGE)
        except LookupError as exc:
            raise CodingSandboxError("sandbox_unavailable") from exc
        args = [
            part
            for part in container_arguments(
                workspace, runtime_image, job_id=job_id, proxy_dir=proxy_dir, job_token=job_token
            )
            if part != "--rm"
        ]
        launched = _docker(["run", "-d", "--name", container_name(job_id), *args[1:]])
    if launched.returncode != 0 and not coding_container_ids(job_id):
        raise CodingSandboxError("sandbox_failed")


def _wait_until_stopped(
    job_id: str,
    *,
    timeout_seconds: int,
    stop_requested: Callable[[], bool] | None,
    deadline_reached: Callable[[], bool] | None,
    on_poll: Callable[[], None] | None,
    grace_seconds: int,
    poll_seconds: float,
) -> dict[str, Any]:
    name = container_name(job_id)
    deadline = time.monotonic() + timeout_seconds
    while True:
        if on_poll is not None:
            on_poll()
        if stop_requested is not None and stop_requested():
            _raise_after_stop(job_id, grace_seconds, "cancelled", "cancel_unconfirmed")
        if deadline_reached is not None and deadline_reached():
            _raise_after_stop(job_id, grace_seconds, "deadline_exceeded", "deadline_unconfirmed")
        status = _container_status(name)
        if status in {"exited", "dead"}:
            return _collect_exit(name)
        if status is None and coding_container_ids(job_id) == []:
            logs = container_log_tails(job_id)
            raise CodingSandboxError("timeout", logs)
        if time.monotonic() >= deadline:
            logs = container_log_tails(job_id)
            if not confirm_coding_stop(job_id, grace_seconds=grace_seconds):
                raise CodingSandboxError("timeout_unconfirmed", logs)
            raise CodingSandboxError("timeout", logs)
        time.sleep(poll_seconds)


def _raise_after_stop(job_id: str, grace_seconds: int, confirmed_code: str, pending_code: str) -> None:
    logs = container_log_tails(job_id)
    if not confirm_coding_stop(job_id, grace_seconds=grace_seconds):
        raise CodingSandboxError(pending_code, logs)
    raise CodingSandboxError(confirmed_code, logs)


def _collect_exit(name: str) -> dict[str, Any]:
    captured = _bounded_logs(name, limit=LOG_CAPTURE_LIMIT)
    logs = {"stdout": captured["stdout"][-LOG_TAIL_LIMIT:], "stderr": captured["stderr"][-LOG_TAIL_LIMIT:]}
    code = _exit_code(name)
    _sandbox_docker(["rm", "-f", name])
    result = _result_line(captured["stdout"])
    if code not in {0, 1} or result is None:
        raise CodingSandboxError("sandbox_failed", logs)
    return {
        "exit_code": code,
        "result": result,
        "timed_out": False,
        "stdout_tail": logs["stdout"],
        "stderr_tail": logs["stderr"],
    }


def _container_status(name: str) -> str | None:
    completed = _sandbox_docker(["inspect", "-f", "{{.State.Status}}", name])
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _exit_code(name: str) -> int | None:
    completed = _sandbox_docker(["inspect", "-f", "{{.State.ExitCode}}", name])
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return None


def _sandbox_docker(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    if not worker_delegates():
        return _docker(args, timeout=timeout)
    try:
        return remote_completed(args, timeout=60 if timeout is None else timeout)
    except SandboxDelegateError as exc:
        raise CodingSandboxError("sandbox_unavailable") from exc


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
