"""Run one coding attempt in a networkless, read-only, resource-bounded container."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests
from docker.errors import DockerException

from aether_worker.container_runtime import container_logs
from aether_worker.grading_files import PROTOCOL_CAP_SECONDS
from aether_worker.openhands_image import (
    IMAGE_UNAVAILABLE,
    RUN_IMAGE_REASONS,
    launch_image,
)
from aether_worker.output_bounds import (  # shared with container_runtime; callers still import them from here
    LOG_CAPTURE_LIMIT,
    MAX_PATCH_BYTES,  # noqa: F401
    RESULT_CAPTURE_LIMIT,
    read_bounded_tail,
)
from aether_worker.runner import JOB_IMAGE, _docker, docker_available
from aether_worker.sandbox_delegate import (
    SandboxDelegateError,
    accepted_objective,
    remote_completed,
    remote_logs,
    remote_run,
    worker_delegates,
)

CANCEL_GRACE_SECONDS = 10
LOG_TAIL_LIMIT = 4096
# The CLI log fallback is killed after this long so a wedged `docker logs` cannot hold the job.
CLI_LOGS_TIMEOUT_SECONDS = 60
POLL_SECONDS = 0.25
# With a heartbeat, a blocking log read is cut into waits of at most this long (LEASE_SECONDS / 3).
LOG_HEARTBEAT_SECONDS = 10.0
# 768 MiB. One agent-entry sample peaked at 283888 KiB, above the old 256 MiB cgroup.
CODING_MEMORY_BYTES = 805306368
# The agent's share (80 s, unchanged) plus the grading protocol's hard cap, the entry's 5 s kill margin
# and 30 s for container start-up and writing the result: the protocol limit now grows with the suite
# (ADR-F44), so the container must outlast its cap. The job lease (30 s) is renewed on every 0.25 s poll
# while the container runs, so a longer container time does not let it lapse.
CODING_SANDBOX_TIMEOUT_SECONDS = 80 + PROTOCOL_CAP_SECONDS + 5 + 30
_MODEL_SOCKET = "/run/aether/proxy/model.sock"


class CodingSandboxError(Exception):
    def __init__(self, code: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.payload = payload or {}


def container_name(job_id: str) -> str:
    token = "".join(character for character in job_id if character.isalnum())[:32]
    return f"aether-code-{token}"


def checked_objective(objective: object) -> str:
    """The objective text, unchanged, or objective_invalid. It is never formatted into a command."""
    try:
        return accepted_objective(objective)
    except SandboxDelegateError as exc:
        raise CodingSandboxError("objective_invalid") from exc


def container_arguments(
    workspace: Path,
    image: str,
    *,
    objective: str,
    job_id: str | None = None,
    proxy_dir: Path | None = None,
    job_token: str | None = None,
) -> list[str]:
    """Flags only. The caller supplies docker and does not inherit the worker environment.

    The objective is one argv element, so no shell sees it; the entry sends it as the user message.
    """
    text = checked_objective(objective)
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
        "-e",
        f"AETHER_OBJECTIVE={text}",
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


def _with_heartbeat(work: Callable[[], Any], heartbeat: Callable[[], None] | None) -> Any:
    """Run a blocking call; with a heartbeat, run it on a reader thread and beat between join slices.

    The call keeps its own timeout. An exception from the heartbeat propagates at once and the
    daemon reader is left to finish on its own; it only reads logs, so nothing is written.
    """
    if heartbeat is None:
        return work()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    reader = threading.Thread(target=run, daemon=True)
    reader.start()
    while True:
        reader.join(LOG_HEARTBEAT_SECONDS)
        if not reader.is_alive():
            break
        heartbeat()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _bounded_logs(target: str, *, limit: int, heartbeat: Callable[[], None] | None = None) -> dict[str, str]:
    """Both streams come back from the Docker SDK. An empty stream is b'', not a missing pipe.

    With a heartbeat, it runs at most every LOG_HEARTBEAT_SECONDS while the logs are read.
    """
    if worker_delegates():
        try:
            return _with_heartbeat(lambda: remote_logs(target, limit, timeout=60), heartbeat)
        except SandboxDelegateError as exc:
            raise CodingSandboxError("sandbox_unavailable") from exc
    try:
        stdout, stderr = _with_heartbeat(lambda: container_logs(target, limit=limit), heartbeat)
    except (DockerException, requests.RequestException):
        # The SDK cannot reach the daemon, or reached it but could not serve this container's
        # logs (containers.get or logs failed, or the connection dropped mid-stream, which
        # requests raises outside DockerException); the docker CLI, if present, is the fallback.
        return _cli_logs(target, limit=limit, heartbeat=heartbeat)
    return {
        "stdout": read_bounded_tail(io.BytesIO(stdout), limit),
        "stderr": read_bounded_tail(io.BytesIO(stderr), min(limit, LOG_CAPTURE_LIMIT)),
    }


def _cli_logs(target: str, *, limit: int, heartbeat: Callable[[], None] | None = None) -> dict[str, str]:
    """`docker logs`, one stream per run so neither pipe can block the other; only the tail is kept.

    Both runs share one CLI_LOGS_TIMEOUT_SECONDS deadline: the second gets only what the first left.
    """
    cli = shutil.which("docker")
    if cli is None:
        return {"stdout": "", "stderr": ""}
    deadline = time.monotonic() + CLI_LOGS_TIMEOUT_SECONDS
    stdout = _cli_stream(cli, target, limit, stdout=True, deadline=deadline, heartbeat=heartbeat)
    stderr = _cli_stream(cli, target, min(limit, LOG_CAPTURE_LIMIT), stdout=False, deadline=deadline, heartbeat=heartbeat)
    return {"stdout": stdout, "stderr": stderr}


def _cli_stream(
    cli: str,
    target: str,
    limit: int,
    *,
    stdout: bool,
    deadline: float,
    heartbeat: Callable[[], None] | None = None,
) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    try:
        process = subprocess.Popen(
            [cli, "logs", target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL if stdout else subprocess.PIPE,
        )
    except OSError:
        return ""
    stream = process.stdout if stdout else process.stderr
    if stream is None:
        process.kill()
        process.wait()
        return ""
    if heartbeat is not None:
        return _cli_stream_beating(process, stream, limit, deadline=deadline, heartbeat=heartbeat)
    timer = threading.Timer(remaining, process.kill)
    timer.start()
    try:
        return read_bounded_tail(stream, limit)
    finally:
        timer.cancel()
        stream.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _cli_stream_beating(
    process: subprocess.Popen[bytes],
    stream: Any,
    limit: int,
    *,
    deadline: float,
    heartbeat: Callable[[], None],
) -> str:
    """The kill timer of _cli_stream as join slices: same deadline, a heartbeat between slices.

    At the deadline the process is killed, its pipe closes and the reader returns the tail it has.
    If the heartbeat raises, the process is killed the same way and the exception propagates.
    """
    box: dict[str, str] = {}
    reader = threading.Thread(target=lambda: box.update(text=read_bounded_tail(stream, limit)), daemon=True)
    reader.start()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                break
            reader.join(min(LOG_HEARTBEAT_SECONDS, remaining))
            if not reader.is_alive():
                break
            if deadline - time.monotonic() > 0:
                heartbeat()
    except BaseException:
        process.kill()
        raise
    finally:
        reader.join()
        stream.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return box.get("text", "")


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
    objective: str,
    image: str = JOB_IMAGE,
    timeout_seconds: int = CODING_SANDBOX_TIMEOUT_SECONDS,
    job_id: str | None = None,
    proxy_dir: Path | None = None,
    job_token: str | None = None,
    stop_requested: Callable[[], bool] | None = None,
    deadline_reached: Callable[[], bool] | None = None,
    on_poll: Callable[[], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
    grace_seconds: int = CANCEL_GRACE_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> dict[str, Any]:
    """heartbeat, if given, keeps the caller's lease alive while the exit logs are read; it may raise."""
    # Starting, polling and stopping all go through the Docker SDK (or the runner when delegated); the
    # docker CLI is only a best-effort log fallback, so its absence does not make the sandbox unavailable.
    if not docker_available():
        raise CodingSandboxError("sandbox_unavailable")
    text = checked_objective(objective)
    chosen = job_id or str(uuid.uuid4())
    _ensure_started(workspace, image, chosen, proxy_dir=proxy_dir, job_token=job_token, objective=text)
    return _wait_until_stopped(
        chosen,
        timeout_seconds=timeout_seconds,
        stop_requested=stop_requested,
        deadline_reached=deadline_reached,
        on_poll=on_poll,
        heartbeat=heartbeat,
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
    objective: str,
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
            launched = remote_run(
                workspace, job_id, proxy_dir=proxy_dir, job_token=job_token or "", objective=objective, timeout=180
            )
        except SandboxDelegateError as exc:
            raise CodingSandboxError("sandbox_unavailable") from exc
        marker = (launched.stderr or "").strip()
        if launched.returncode != 0 and (marker == IMAGE_UNAVAILABLE or marker in RUN_IMAGE_REASONS):
            # The runner has no usable image: prep failed before the roles started, or the run's
            # image could not be pulled or lacked its label. The precise reason rides in the payload.
            raise CodingSandboxError("image_unavailable", {"reason": marker})
    else:
        try:
            runtime_image = launch_image(image, JOB_IMAGE)
        except LookupError as exc:
            raise CodingSandboxError("image_unavailable") from exc
        args = [
            part
            for part in container_arguments(
                workspace, runtime_image, objective=objective, job_id=job_id, proxy_dir=proxy_dir, job_token=job_token
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
    heartbeat: Callable[[], None] | None = None,
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
            return _collect_exit(name, heartbeat=heartbeat)
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


def _collect_exit(name: str, *, heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    captured = _bounded_logs(name, limit=RESULT_CAPTURE_LIMIT, heartbeat=heartbeat)
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
