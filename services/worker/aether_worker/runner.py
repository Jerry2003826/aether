"""Bounded Linux job runner. Docker is unsupported until a real daemon probe passes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reference.models import CodingJobRequest, CodingJobResult

PROBE = Path(__file__).with_name("namespace_probe.py")
NETWORK_UNREACHABLE = 101


class RunnerError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def job_environment(*, token: str, gateway_url: str) -> dict[str, str]:
    """The job sees a gateway token, not provider or admin credentials."""
    return {"PATH": os.environ.get("PATH", "/usr/bin"), "AETHER_JOB_TOKEN": token, "AETHER_GATEWAY_URL": gateway_url}


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
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode())


def probe_linux_isolation(hide_path: Path) -> dict[str, Any]:
    """Measure this process's namespaces. A denied unshare is not_supported, not a pass."""
    hide_path.mkdir(parents=True, exist_ok=True)
    marker = hide_path / "marker"
    marker.write_text("host-only", encoding="utf-8")
    env = os.environ.copy()
    env.pop("AETHER_PROVIDER_KEY", None)
    env["AETHER_HIDE_PATH"] = str(hide_path)
    docker = "not_supported"
    if shutil.which("docker"):
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
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
                "python3",
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
        if existing is not None:
            return _result(existing)
        if provider_key and provider_key in token:
            raise RunnerError("secret_in_token")
        plan = openhands_launch_plan(token=token, gateway_url=gateway_url)
        if provider_key and provider_key in json.dumps(plan):
            raise RunnerError("secret_in_plan")
        job_id = f"job-{request.intent_id}"
        record = {
            "job_id": job_id,
            "project_id": request.project_id,
            "task_id": request.task_id,
            "intent_id": request.intent_id,
            "status": "ACTIVE",
            "spawn_count": 1,
            "external_effect": "unknown",
            "environment": job_environment(token=token, gateway_url=gateway_url),
            "plan": {key: value for key, value in plan.items() if key != "token"},
        }
        if provider_key and provider_key in json.dumps(record):
            raise RunnerError("secret_in_job")
        self._jobs[key] = record
        self._save()
        return _result(record)

    def get(self, project_id: str, job_id: str) -> CodingJobResult:
        record = self._find(project_id, job_id)
        return _result(record)

    def cancel(self, project_id: str, job_id: str) -> CodingJobResult:
        record = self._find(project_id, job_id)
        record["status"] = "CANCELLED"
        record["external_effect"] = "unknown"
        self._save()
        return _result(record)

    def _find(self, project_id: str, job_id: str) -> dict[str, Any]:
        for record in self._jobs.values():
            if record["project_id"] == project_id and record["job_id"] == job_id:
                return record
        raise RunnerError("job_missing")


def _result(record: dict[str, Any]) -> CodingJobResult:
    return CodingJobResult(job_id=record["job_id"], status=record["status"])
