"""Run one coding attempt in a networkless, read-only, resource-bounded container."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from aether_domain.coding_limits import CODING_ITERATIONS_ENV, coding_max_iterations
from docker.errors import DockerException

from aether_worker.container_runtime import LEFTOVER_INSPECT_TEMPLATE, container_logs
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
from aether_worker.secret_scan import redact_text

CANCEL_GRACE_SECONDS = 10
LOG_TAIL_LIMIT = 4096
# The CLI log fallback is killed after this long so a wedged `docker logs` cannot hold the job.
CLI_LOGS_TIMEOUT_SECONDS = 60
POLL_SECONDS = 0.25
_LOG = logging.getLogger("aether.worker.coding")
# With a heartbeat, a blocking log read is cut into waits of at most this long (LEASE_SECONDS / 3).
LOG_HEARTBEAT_SECONDS = 10.0
# 768 MiB. One agent-entry sample peaked at 283888 KiB, above the old 256 MiB cgroup.
CODING_MEMORY_BYTES = 805306368
# The agent's share (80 s, unchanged) plus the grading protocol's hard cap, the entry's 5 s kill margin
# and 30 s for container start-up and writing the result: the protocol limit now grows with the suite
# (ADR-F44), so the container must outlast its cap. The job lease (30 s) is renewed on every 0.25 s poll
# while the container runs, so a longer container time does not let it lapse.
CODING_SANDBOX_TIMEOUT_SECONDS = 80 + PROTOCOL_CAP_SECONDS + 5 + 30
# A stopped coding container older than this is left over (its worker collected it, or never will):
# far past the time any worker needs between seeing the exit and removing the container.
LEFTOVER_GRACE_SECONDS = 600
_DEPLOYMENT_ENV = "AETHER_DEPLOYMENT_ID"
_ZERO_TIME = "0001-01-01T00:00:00Z"
_INSTANCE_ENV = "AETHER_WORKER_INSTANCE_ID"
_STATE_DIR_ENV = "AETHER_WORKER_STATE_DIR"
_INSTANCE_FILE = "coding-instance-id"
_INSTANCE_LABEL = "aether.worker-instance"
# Only these states have exited; created (never started), running, paused, restarting are never swept.
_EXITED = ("exited", "dead")
_unpersisted_instance: str | None = None


def _clean_label_value(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())[:64]


def pin_worker_state_dir() -> Path:
    """Resolve the worker state directory once, against this process's working directory, and write
    the absolute path back to AETHER_WORKER_STATE_DIR. Called when the worker starts, before the
    runner is launched: the runner inherits the absolute path (role_launch._runner_env), so it finds
    the same coding-instance-id whatever its own working directory."""
    path = Path(os.environ.get(_STATE_DIR_ENV, "") or "data/worker")
    resolved = path if path.is_absolute() else (Path.cwd() / path).resolve()
    os.environ[_STATE_DIR_ENV] = str(resolved)
    return resolved


def worker_instance_id() -> str:
    """This worker's instance id, read fresh each time: AETHER_WORKER_INSTANCE_ID, else the id kept in
    <AETHER_WORKER_STATE_DIR, default data/worker>/coding-instance-id (created once, so a restarted
    worker with the same state directory finds the containers its previous run left). If the file
    cannot be written, a random id for this process only: it then never claims an earlier run's
    containers. Workers that run at the same time must not share a state directory."""
    global _unpersisted_instance
    explicit = _clean_label_value(os.environ.get(_INSTANCE_ENV, ""))
    if explicit:
        return explicit
    path = Path(os.environ.get(_STATE_DIR_ENV, "data/worker")) / _INSTANCE_FILE
    try:
        stored = _clean_label_value(path.read_text(encoding="ascii"))
        if stored:
            return stored
    except (OSError, UnicodeDecodeError):
        pass
    fresh = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="ascii") as handle:
            handle.write(fresh + "\n")
        return fresh
    except FileExistsError:
        # Another process of the same worker created it first; use what it wrote.
        stored = _clean_label_value(path.read_text(encoding="ascii"))
        if stored:
            return stored
    except OSError:
        pass
    _LOG.warning("coding_instance_id_unpersisted path=%s", path)
    if _unpersisted_instance is None:
        _unpersisted_instance = fresh
    return _unpersisted_instance


def instance_label() -> str:
    return f"{_INSTANCE_LABEL}={worker_instance_id()}"


def deployment_label() -> str:
    """aether.deployment=<AETHER_DEPLOYMENT_ID, else local>: for operators listing a stack's
    containers. The startup sweep never selects by it (see instance_label)."""
    value = _clean_label_value(os.environ.get(_DEPLOYMENT_ENV, ""))
    return f"aether.deployment={value or 'local'}"
_MODEL_SOCKET = "/run/aether/proxy/model.sock"
# The proxy (directory, job token) each container this process started is wired to, and the id
# `docker run -d` answered for it ("" if it named none), by job id. A run always gets a fresh proxy
# directory and token, so a container found under any other key was started by a worker whose
# proxy and socket are gone.
_STARTED_ON: dict[str, tuple[str, str, str]] = {}
_STARTED_LOCK = threading.Lock()
# What `docker run -d` prints on success: the new container's id (64 hex characters from the CLI).
_STARTED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


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
    try:
        # The entry's only source for its step limit, which is also the gateway's call cap.
        iterations = coding_max_iterations()
    except ValueError:
        raise CodingSandboxError("coding_max_iterations_invalid") from None
    labels: list[str] = []
    if job_id is not None:
        labels = ["--label", f"aether.coding={job_id}", "--label", "aether.role=coding", "--label", deployment_label(), "--label", instance_label()]
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
        "AETHER_SANDBOX_SHM=/dev/shm",
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
        "-e",
        f"{CODING_ITERATIONS_ENV}={iterations}",
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


def _listed(container_id: str, listing: list[str]) -> bool:
    """`docker ps -q` prints 12-character short ids; `docker run -d` printed the full one."""
    return any(item and (container_id.startswith(item) or item.startswith(container_id)) for item in listing)


def _tail(text: str) -> str:
    """The stored tail, redacted over the whole text (secret_scan.redact_text). When the tail cuts
    the log, its partial first line goes: a secret cut in half there no longer matches any detector."""
    tail = text[-LOG_TAIL_LIMIT:]
    if len(text) > LOG_TAIL_LIMIT:
        newline = tail.find("\n")
        tail = "[cut]" + (tail[newline:] if newline >= 0 else "")
    return redact_text(tail)


def _log_tails(captured: dict[str, str]) -> dict[str, str]:
    """The stored log tails, each redacted with secret_scan.redact_text before it is kept anywhere.
    The result is parsed from the raw capture, never from these."""
    return {"stdout": _tail(captured["stdout"]), "stderr": _tail(captured["stderr"])}


def container_log_tails(job_id: str, container_id: str | None = None) -> dict[str, str]:
    if container_id:
        target = container_id
    else:
        found = coding_container_ids(job_id)
        target = found[0] if found else container_name(job_id)
    return _log_tails(_bounded_logs(target, limit=LOG_CAPTURE_LIMIT))


def confirm_coding_stop(
    job_id: str, *, grace_seconds: int = CANCEL_GRACE_SECONDS, container_id: str | None = None
) -> bool:
    """True only when a second listing shows no container for this job.

    With container_id (the one this worker started), only that container is stopped and removed,
    and True means the listing no longer shows it. Another worker may have taken the job over and
    started its own under the same name and labels; that one is left running. Without it (the
    reaper, or a claim that stops a due job before starting one), every container of the job goes.
    """
    if coding_container_ids(job_id) is None:
        return False
    if container_id:
        _sandbox_docker(["stop", "-t", str(grace_seconds), container_id])
        _sandbox_docker(["rm", "-f", container_id])
        remaining = coding_container_ids(job_id)
        return remaining is not None and not _listed(container_id, remaining)
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
    started = _ensure_started(workspace, image, chosen, proxy_dir=proxy_dir, job_token=job_token, objective=text)
    return _wait_until_stopped(
        chosen,
        container_id=started,
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
) -> str | None:
    """Start the job's container, or attach to the one this run already started; return its id.

    The id is None only when Docker named none; the stop paths then fall back to the job's name.
    """
    existing = coding_container_ids(job_id)
    if existing is None:
        raise CodingSandboxError("sandbox_unavailable")
    wired = (str(proxy_dir.resolve()) if proxy_dir is not None else "", job_token or "")
    if existing:
        with _STARTED_LOCK:
            record = _STARTED_ON.get(job_id)
        if record is not None and record[:2] == wired and (not record[2] or _listed(record[2], existing)):
            return record[2] or None
        # Left by a worker that died: it still talks to that worker's socket, which nothing serves.
        # Its usage so far is already on the job's reservation row; the agent reruns from the base
        # artifact on this run's proxy, and the publish stays fenced. This takeover is the only
        # place a container is removed by the job's name and labels rather than by its own id.
        _LOG.warning("coding_stale_container_replaced job=%s", job_id)
        release_coding_container(job_id)
        if coding_container_ids(job_id) != []:
            raise CodingSandboxError("stale_container_unconfirmed")
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
    listing = None if launched.returncode == 0 else coding_container_ids(job_id)
    if launched.returncode != 0 and not listing:
        raise CodingSandboxError("sandbox_failed")
    started = _started_id(launched.stdout if launched.returncode == 0 else "")
    if started is None:
        # `docker run -d` created the container but named no id (or failed after creating it):
        # right after the replacement above, the one container listed for the job is this one.
        listing = coding_container_ids(job_id) if listing is None else listing
        started = listing[0] if listing is not None and len(listing) == 1 else None
    if launched.returncode == 0:
        with _STARTED_LOCK:
            _STARTED_ON[job_id] = (*wired, started or "")
    return started


def _started_id(stdout: str | None) -> str | None:
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines or _STARTED_ID.fullmatch(lines[-1]) is None:
        return None
    return lines[-1]


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
    container_id: str | None = None,
) -> dict[str, Any]:
    """Poll the container this run started until it exits, is stopped, or the run gives up.

    container_id is the id _ensure_started returned. Every status read, log read and removal here
    targets that id, never the job's name or labels: a worker that lost its lease may still be
    polling after another worker replaced its container and started one under the same name.
    Without an id (Docker named none) the name and labels are the fallback, as before.
    """
    target = container_id or container_name(job_id)
    deadline = time.monotonic() + timeout_seconds
    while True:
        if on_poll is not None:
            try:
                on_poll()
            except BaseException:
                # The caller gave up the run (its lease is gone): the agent must not keep working.
                _remove_coding_container(job_id, container_id)
                raise
        if stop_requested is not None and stop_requested():
            _raise_after_stop(job_id, grace_seconds, "cancelled", "cancel_unconfirmed", container_id=container_id)
        if deadline_reached is not None and deadline_reached():
            _raise_after_stop(job_id, grace_seconds, "deadline_exceeded", "deadline_unconfirmed", container_id=container_id)
        status = _container_status(target)
        if status in {"exited", "dead"}:
            return _collect_exit(target, heartbeat=heartbeat)
        if status is None and _gone(job_id, container_id):
            logs = container_log_tails(job_id, container_id)
            raise CodingSandboxError("timeout", logs)
        if time.monotonic() >= deadline:
            logs = container_log_tails(job_id, container_id)
            if not confirm_coding_stop(job_id, grace_seconds=grace_seconds, container_id=container_id):
                raise CodingSandboxError("timeout_unconfirmed", logs)
            raise CodingSandboxError("timeout", logs)
        time.sleep(poll_seconds)


def _gone(job_id: str, container_id: str | None) -> bool:
    listing = coding_container_ids(job_id)
    if not container_id:
        return listing == []
    return listing is not None and not _listed(container_id, listing)


def _remove_coding_container(job_id: str, container_id: str | None = None) -> None:
    """Best effort: a failure here must not replace the error that stopped the run.

    With container_id only that container goes; another worker's container for the job stays.
    """
    try:
        if container_id:
            _sandbox_docker(["rm", "-f", container_id])
        else:
            release_coding_container(job_id)
    except Exception:  # noqa: BLE001 - best effort, the caller's error is the one that matters
        _LOG.warning("coding_container_remove_failed job=%s", job_id, exc_info=True)


def _raise_after_stop(
    job_id: str, grace_seconds: int, confirmed_code: str, pending_code: str, *, container_id: str | None = None
) -> None:
    logs = container_log_tails(job_id, container_id)
    if not confirm_coding_stop(job_id, grace_seconds=grace_seconds, container_id=container_id):
        raise CodingSandboxError(pending_code, logs)
    raise CodingSandboxError(confirmed_code, logs)


def _remove_after_exit(name: str) -> None:
    """rm -f, once more if it fails; a container still left is logged and swept at the next start."""
    for _attempt in range(2):
        if _sandbox_docker(["rm", "-f", name]).returncode == 0:
            return
    _LOG.warning("coding_container_left name=%s", name)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    text = value.strip().replace("Z", "+00:00")
    # Docker writes nanoseconds; datetime takes at most microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _leftover(details: dict[str, Any], now: datetime) -> bool:
    """Exited (or dead) longer than the grace ago. Running, paused, restarting or never started: no."""
    state = details.get("State") if isinstance(details.get("State"), dict) else {}
    if str(state.get("Status") or "") not in _EXITED:
        return False
    stopped = _parse_time(state.get("FinishedAt")) or _parse_time(state.get("StartedAt")) or _parse_time(details.get("Created"))
    return stopped is not None and (now - stopped).total_seconds() > LEFTOVER_GRACE_SECONDS


def sweep_leftover_coding_containers(*, now: datetime | None = None) -> list[str]:
    """Remove coding containers this worker instance started that have exited; the ids removed.

    Run by the runner role when it starts, before it serves; never in the worker process. A container
    that outlived its job (an unconfirmed stop, a failed rm -f, a worker that died before removing it)
    keeps the job's output in Docker's own log until removed. Several stacks share a Docker daemon, so
    only containers labelled aether.role=coding and this worker's instance_label() (persisted, so a
    restart still matches), never by name or deployment label; and only exited or dead ones past
    LEFTOVER_GRACE_SECONDS, state checked here as well as by docker's filter. A running container is
    never removed. Best effort: nothing here stops the runner from starting. A process in the worker
    role (it delegates Docker to the runner) never sweeps.
    """
    if worker_delegates():
        _LOG.info("coding_container_sweep skipped=delegated")
        return []
    moment = now or datetime.now(UTC)
    removed: list[str] = []
    try:
        if not docker_available():
            return []
        filters = ["--filter", "label=aether.role=coding", "--filter", f"label={instance_label()}"]
        for status in _EXITED:
            filters += ["--filter", f"status={status}"]
        listing = _sandbox_docker(["ps", "-aq", *filters])
        if listing.returncode != 0:
            return []
        for container in listing.stdout.split():
            inspected = _sandbox_docker(["inspect", "-f", LEFTOVER_INSPECT_TEMPLATE, container])
            if inspected.returncode != 0:
                continue
            try:
                details = json.loads(inspected.stdout)
            except json.JSONDecodeError:
                continue
            if not isinstance(details, dict) or not _leftover(details, moment):
                continue
            if _sandbox_docker(["rm", "-f", container]).returncode == 0:
                removed.append(container)
    except Exception:  # noqa: BLE001 - best effort; the worker starts either way
        _LOG.warning("coding_container_sweep_failed", exc_info=True)
    if removed:
        _LOG.warning("coding_container_sweep removed=%d ids=%s", len(removed), ",".join(removed))
    return removed


def _collect_exit(name: str, *, heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    captured = _bounded_logs(name, limit=RESULT_CAPTURE_LIMIT, heartbeat=heartbeat)
    logs = _log_tails(captured)
    code = _exit_code(name)
    _remove_after_exit(name)
    result = _result_line(captured["stdout"])
    if code == 2 and _runner_detail(result) == PATCH_UNAVAILABLE:
        # The entry's git could not write the agent's patch: its own code, not sandbox_failed and
        # never agent_no_change (there is no patch, which says nothing about the agent's change).
        raise CodingSandboxError(PATCH_UNAVAILABLE, logs)
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


PATCH_UNAVAILABLE = "patch_unavailable"


def _runner_detail(result: dict[str, Any] | None) -> str | None:
    """The detail of the entry's single runner case (its own failures), if that is all it reported."""
    cases = result.get("cases") if isinstance(result, dict) else None
    if isinstance(cases, list) and len(cases) == 1 and isinstance(cases[0], dict) and cases[0].get("name") == "runner":
        detail = cases[0].get("detail")
        return detail if isinstance(detail, str) else None
    return None


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
