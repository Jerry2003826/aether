"""Bounded Linux job runner. Docker is unsupported until a real daemon probe passes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reference.models import CodingJobRequest, CodingJobResult

from aether_worker.container_runtime import execute_docker
from aether_worker.loopback import LoopbackError, get_json
from aether_worker.sandbox_image import SANDBOX_IMAGE

PROBE = Path(__file__).with_name("namespace_probe.py")
NETWORK_UNREACHABLE = 101
JOB_IMAGE = SANDBOX_IMAGE
_PROCESS_ROLE = "AETHER_PROCESS_ROLE"
_WORKER_ROLE = "worker"
_RUNTIME = "runtime"
_ENABLED = "enabled"
_UNSUPPORTED = "unsupported"
# An empty proxy map ignores HTTP_PROXY so a loopback bearer is not forwarded.
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GATEWAY_PROGRAM = (
    "import fcntl,json,os,socket,struct\n"
    "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
    "token=os.environ['AETHER_EXPECTED_TOKEN']\n"
    "link=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
    "current=fcntl.ioctl(link.fileno(), 0x8913, struct.pack('16sH14s', b'lo', 0, b''))\n"
    "flags=struct.unpack('16sH', current[:18])[1]\n"
    "fcntl.ioctl(link.fileno(), 0x8914, struct.pack('16sH14s', b'lo', flags|1, b''))\n"
    "print('loopback-up', flush=True)\n"
    "class Handler(BaseHTTPRequestHandler):\n"
    "    def do_POST(self):\n"
    "        size=int(self.headers.get('content-length','0'))\n"
    "        body=self.rfile.read(size)\n"
    "        if self.headers.get('authorization')!='Bearer '+token:\n"
    "            self.send_response(403); self.end_headers(); return\n"
    "        data=json.dumps({'output':'scripted','usage_status':'scripted'}).encode()\n"
    "        self.send_response(200)\n"
    "        self.send_header('content-type','application/json')\n"
    "        self.send_header('content-length',str(len(data)))\n"
    "        self.end_headers(); self.wfile.write(data)\n"
    "    def log_message(self, format, *args):\n"
    "        return\n"
    "server=ThreadingHTTPServer(('127.0.0.1',8080), Handler)\n"
    "print('serving', server.server_address, flush=True)\n"
    "server.serve_forever()\n"
)


class RunnerError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def job_environment(*, token: str, gateway_url: str) -> dict[str, str]:
    """Whitelist. The job process does not inherit the worker environment."""
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "AETHER_JOB_TOKEN": token,
        "AETHER_GATEWAY_URL": gateway_url,
    }


def openhands_launch_plan(*, token: str, gateway_url: str) -> dict[str, Any]:
    sdk = "not_loaded"
    try:
        import openhands  # type: ignore[import-not-found]
    except ImportError:
        sdk = "not_loaded"
    else:
        sdk = getattr(openhands, "__version__", "loaded")
    return {
        "sdk": sdk,
        "llm_base_url": gateway_url,
        "api_key_env": "AETHER_JOB_TOKEN",
        "token": token,
        "mounts": [],
        "docker_socket": False,
        "dependency_install": "prebuilt_only",
    }


def call_gateway(env: dict[str, str], *, prompt: str) -> dict[str, Any]:
    request = urllib.request.Request(
        env["AETHER_GATEWAY_URL"],
        data=json.dumps({"alias": "coder", "prompt": prompt}).encode(),
        headers={"authorization": f"Bearer {env['AETHER_JOB_TOKEN']}", "content-type": "application/json"},
        method="POST",
    )
    with _NO_PROXY.open(request, timeout=5) as response:
        return json.loads(response.read().decode())


def probe_linux_isolation(hide_path: Path) -> dict[str, Any]:
    """Measure this process's namespaces. A denied unshare is not_supported, not a pass."""
    hide_path.mkdir(parents=True, exist_ok=True)
    marker = hide_path / "marker"
    marker.write_text("host-only", encoding="utf-8")
    env = job_environment(token="probe", gateway_url="http://127.0.0.1:9/v1")
    env["AETHER_HIDE_PATH"] = str(hide_path)
    env["PATH"] = f"{Path(sys.executable).resolve().parent}:{env['PATH']}"
    docker = "not_supported"
    if os.environ.get(_PROCESS_ROLE) != _WORKER_ROLE and shutil.which("docker"):
        info = _docker(["info"])
        docker = "present" if info.returncode == 0 else "not_supported"
    try:
        completed = subprocess.run(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--net",
                "--mount",
                "--pid",
                "--fork",
                "--mount-proc",
                sys.executable,
                str(PROBE),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "not_supported", "reason": type(exc).__name__, "docker": docker}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {"status": "not_supported", "reason": completed.stderr[-500:], "docker": docker, "returncode": completed.returncode}
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    isolated = payload["pid"] == 1 and payload["network_errno"] == NETWORK_UNREACHABLE and payload["host_marker_hidden"] is True and payload["provider_key_present"] is False
    return {
        "status": "probed" if isolated else "failed",
        "docker": docker,
        "network_errno": payload["network_errno"],
        "pid": payload["pid"],
        "host_marker_hidden": payload["host_marker_hidden"],
        "provider_key_present": payload["provider_key_present"],
        "measured_at": datetime.now(UTC).isoformat(),
    }


def _docker(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    if os.environ.get(_PROCESS_ROLE) == _WORKER_ROLE:
        raise RunnerError("docker_forbidden")
    return execute_docker(args, timeout=timeout)


def probe_docker_daemon() -> dict[str, str]:
    """Enable the runner only when the daemon reports a server version.

    Every runner Docker call goes through the SDK (container_runtime), so the SDK reaching the daemon
    is enough; an image with the SDK and a mounted socket but no docker CLI is enabled. `cli_missing`
    is reported only when the SDK cannot reach a daemon and there is no CLI either.
    """
    if os.environ.get(_PROCESS_ROLE) == _WORKER_ROLE:
        return _runtime(_UNSUPPORTED, "docker_forbidden")
    try:
        completed = _docker(["info", "--format", "{{.ServerVersion}}"])
    except OSError:
        completed = subprocess.CompletedProcess(["docker", "info"], 1, "", "sdk_unreachable")
    version = completed.stdout.strip()
    if completed.returncode == 0 and version:
        return {_RUNTIME: _ENABLED, "server_version": version}
    if shutil.which("docker") is None:
        return _runtime(_UNSUPPORTED, "cli_missing")
    return _runtime(_UNSUPPORTED, "daemon_unreachable")


def _runtime(state: str, reason: str) -> dict[str, str]:
    return {_RUNTIME: state, "reason": reason}


def docker_available() -> bool:
    """The worker asks the runner. Every other role probes the daemon itself."""
    if os.environ.get(_PROCESS_ROLE) == _WORKER_ROLE:
        return _runner_enabled()
    return probe_docker_daemon()[_RUNTIME] == _ENABLED


def _runner_enabled() -> bool:
    base = os.environ.get("AETHER_RUNNER_BASE", "").rstrip("/")
    if not base:
        return False
    try:
        body = get_json(f"{base}/health", timeout=10)
    except LoopbackError:
        return False
    return body.get(_RUNTIME) == _ENABLED


def _job_label(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def _container_id(key: str) -> str | None:
    label = _job_label(key)
    completed = _docker(["ps", "-aq", "--filter", f"label=aether.job={label}", "--filter", "label=aether.role=job"])
    if completed.returncode != 0:
        return None
    found = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(found) > 1:
        raise RunnerError("duplicate_container")
    return found[0] if found else None


def release_containers(key: str) -> None:
    if not docker_available():
        return
    label = _job_label(key)
    for role in ("job", "gateway"):
        completed = _docker(["ps", "-aq", "--filter", f"label=aether.job={label}", "--filter", f"label=aether.role={role}"])
        for item in completed.stdout.split():
            _docker(["rm", "-f", item])


def _labelled_containers(key: str) -> list[str] | None:
    """Every container of this job in any state; None when Docker cannot answer."""
    completed = _docker(["ps", "-aq", "--filter", f"label=aether.job={_job_label(key)}"])
    if completed.returncode != 0:
        return None
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _ensure_running(container_id: str) -> None:
    state = _docker(["inspect", "-f", "{{.State.Running}}", container_id])
    if state.returncode != 0:
        raise RunnerError("container_missing")
    if state.stdout.strip() != "true":
        started = _docker(["start", container_id])
        if started.returncode != 0:
            raise RunnerError("container_restart_rejected")


def _ensure_image() -> None:
    if _docker(["image", "inspect", JOB_IMAGE]).returncode == 0:
        return
    pulled = _docker(["pull", JOB_IMAGE])
    if pulled.returncode != 0:
        raise RunnerError("image_unavailable")


def _launch_container(key: str, *, token: str, provider_key: str) -> str:
    """The job shares only the gateway's loopback. That namespace has no external route."""
    label = _job_label(key)
    gateway_name = f"aether-gw-{label}"
    job_name = f"aether-job-{label}"
    _ensure_image()
    if _docker(["inspect", gateway_name]).returncode != 0:
        gateway = _docker(
            [
                "run",
                "-d",
                "--name",
                gateway_name,
                "--network",
                "none",
                # Only NET_ADMIN, to bring up the shared loopback; nothing else, and no setuid gains.
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--security-opt",
                "no-new-privileges",
                "--label",
                f"aether.job={label}",
                "--label",
                "aether.role=gateway",
                "-e",
                f"AETHER_EXPECTED_TOKEN={token}",
                JOB_IMAGE,
                "python",
                "-c",
                GATEWAY_PROGRAM,
            ]
        )
        if gateway.returncode != 0:
            raise RunnerError("gateway_rejected")
    env = job_environment(token=token, gateway_url="http://127.0.0.1:8080/complete")
    command = [
        "run",
        "-d",
        "--name",
        job_name,
        "--network",
        f"container:{gateway_name}",
        "--label",
        f"aether.job={label}",
        "--label",
        "aether.role=job",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/run/aether:rw,nosuid,nodev,noexec,mode=0700",
        "-e",
        "HOME=/run/aether",
        "-e",
        "TMPDIR=/run/aether",
        "-e",
        f"AETHER_JOB_TOKEN={env['AETHER_JOB_TOKEN']}",
        "-e",
        f"AETHER_GATEWAY_URL={env['AETHER_GATEWAY_URL']}",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        JOB_IMAGE,
        "sleep",
        "180",
    ]
    if provider_key and any(provider_key in part for part in command):
        raise RunnerError("secret_in_job")
    launched = _docker(command)
    if launched.returncode != 0:
        raise RunnerError("container_rejected")
    found = _container_id(key)
    if found is None:
        raise RunnerError("container_missing")
    return found


class LinuxRunner:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = state_dir / "jobs.json"
        self._jobs: dict[str, dict[str, Any]] = {}
        if self._index_path.exists():
            self._jobs = json.loads(self._index_path.read_text(encoding="utf-8"))

    def _key(self, request: CodingJobRequest) -> str:
        return f"{request.project_id}:{request.task_id}:{request.intent_id}"

    def _save(self) -> None:
        temporary = self._index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._jobs, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self._index_path)

    def start(self, request: CodingJobRequest, *, token: str, gateway_url: str, provider_key: str) -> CodingJobResult:
        key = self._key(request)
        existing = self._jobs.get(key)
        enabled = probe_docker_daemon()[_RUNTIME] == _ENABLED
        found = _container_id(key) if enabled else None
        if existing is not None and existing.get("status") == "CANCELLED":
            return self._finish_cancelled(key, existing)
        if existing is not None:
            return self._reuse_existing(existing, found)
        if found is not None:
            return self._adopt_found(key, request, found, token=token, gateway_url=gateway_url, provider_key=provider_key)
        if provider_key and provider_key in token:
            raise RunnerError("secret_in_token")
        record = self._new_record(request, token=token, gateway_url=gateway_url, provider_key=provider_key)
        record[_RUNTIME] = _ENABLED if enabled else _UNSUPPORTED
        if enabled:
            record["container_id"] = _launch_container(key, token=token, provider_key=provider_key)
        self._jobs[key] = record
        self._save()
        return _result(record)

    def _finish_cancelled(self, key: str, existing: dict[str, Any]) -> CodingJobResult:
        if existing.get("cancel_state") != "confirmed":
            self._stop(key, existing)
        return _result(existing)

    def _reuse_existing(self, existing: dict[str, Any], found: str | None) -> CodingJobResult:
        if found and not existing.get("container_id"):
            existing["container_id"] = found
            self._save()
        if existing.get("container_id"):
            _ensure_running(str(existing["container_id"]))
        return _result(existing)

    def _adopt_found(
        self,
        key: str,
        request: CodingJobRequest,
        found: str,
        *,
        token: str,
        gateway_url: str,
        provider_key: str,
    ) -> CodingJobResult:
        record = self._new_record(request, token=token, gateway_url=gateway_url, provider_key=provider_key)
        record["container_id"] = found
        record[_RUNTIME] = _ENABLED
        self._jobs[key] = record
        self._save()
        _ensure_running(found)
        return _result(record)

    def _new_record(self, request: CodingJobRequest, *, token: str, gateway_url: str, provider_key: str) -> dict[str, Any]:
        plan = openhands_launch_plan(token=token, gateway_url=gateway_url)
        if provider_key and provider_key in json.dumps(plan):
            raise RunnerError("secret_in_plan")
        record = {
            "job_id": f"job-{request.intent_id}",
            "project_id": request.project_id,
            "task_id": request.task_id,
            "intent_id": request.intent_id,
            "status": "ACTIVE",
            "spawn_count": 1,
            "external_effect": "unknown",
            "environment": job_environment(token=token, gateway_url=gateway_url),
            "plan": {name: value for name, value in plan.items() if name != "token"},
        }
        if provider_key and provider_key in json.dumps(record):
            raise RunnerError("secret_in_job")
        return record

    def get(self, project_id: str, job_id: str) -> CodingJobResult:
        record = self._find(project_id, job_id)
        return _result(record)

    def cancel(self, project_id: str, job_id: str) -> CodingJobResult:
        """Revoke the job token, remove the job and gateway containers, then confirm none remain.

        cancel_state stays "requested" and RunnerError("cancel_unconfirmed") is raised while a
        container may still be running. Cancellation is not rollback: external_effect stays unknown.
        """
        for key, record in self._jobs.items():
            if record["project_id"] == project_id and record["job_id"] == job_id:
                self._stop(key, record)
                return _result(record)
        raise RunnerError("job_missing")

    def _stop(self, key: str, record: dict[str, Any]) -> None:
        environment = record.get("environment") or {}
        if environment.pop("AETHER_JOB_TOKEN", None) is not None:
            record["token_revoked"] = True
        record["status"] = "CANCELLED"
        record["external_effect"] = "unknown"
        record["cancel_state"] = "requested"
        self._save()
        if docker_available():
            release_containers(key)
            stopped = _labelled_containers(key) == []
        else:
            stopped = not record.get("container_id")
        if not stopped:
            raise RunnerError("cancel_unconfirmed")
        record["cancel_state"] = "confirmed"
        self._save()

    def _find(self, project_id: str, job_id: str) -> dict[str, Any]:
        for record in self._jobs.values():
            if record["project_id"] == project_id and record["job_id"] == job_id:
                return record
        raise RunnerError("job_missing")


def _result(record: dict[str, Any]) -> CodingJobResult:
    return CodingJobResult(job_id=record["job_id"], status=record["status"])
