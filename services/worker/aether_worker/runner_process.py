"""Runner process. Docker control stays here, not in the worker process."""

from __future__ import annotations

import os
import subprocess

from aether_worker.coding_sandbox import LOG_CAPTURE_LIMIT, _bounded_logs, container_arguments, container_name
from aether_worker.role_http import serve_role
from aether_worker.runner import JOB_IMAGE, _docker, probe_docker_daemon
from aether_worker.sandbox_delegate import (
    SandboxDelegateError,
    accepted_grace,
    accepted_job,
    accepted_name,
    accepted_workspace,
)


def main() -> None:
    token = os.environ.pop("AETHER_LOOPBACK_TOKEN", "")
    if not token:
        raise SystemExit("runner 缺少 AETHER_LOOPBACK_TOKEN")
    os.environ["AETHER_PROCESS_ROLE"] = "runner"
    started = probe_docker_daemon()

    def health() -> dict[str, str]:
        return {"role": "runner", **started}

    def post(path: str, body: dict) -> tuple[int, dict]:
        if path == "/v1/probe":
            return 200, probe_docker_daemon()
        if path == "/v1/sandbox":
            return _sandbox(body)
        return 404, {"error": "not_found"}

    serve_role(token, health, post)


def _sandbox(body: dict) -> tuple[int, dict]:
    op = body.get("op")
    try:
        if op == "logs":
            return 200, _logs(body.get("name"))
        completed = _completed(op, body)
    except SandboxDelegateError:
        return 400, {"error": "rejected"}
    if completed is None:
        return 404, {"error": "not_found"}
    return 200, {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def _completed(op: object, body: dict) -> subprocess.CompletedProcess[str] | None:
    if op == "ps":
        job = accepted_job(body.get("job_id"))
        return _docker(["ps", "-aq", "--filter", f"label=aether.coding={job}", "--filter", "label=aether.role=coding"])
    if op == "run":
        return _run(body.get("workspace"), body.get("job_id"))
    if op == "stop":
        return _docker(["stop", "-t", accepted_grace(body.get("grace")), accepted_name(body.get("name"))])
    if op == "rm":
        return _docker(["rm", "-f", accepted_name(body.get("name"))])
    if op == "inspect_status":
        return _docker(["inspect", "-f", "{{.State.Status}}", accepted_name(body.get("name"))])
    if op == "inspect_exit":
        return _docker(["inspect", "-f", "{{.State.ExitCode}}", accepted_name(body.get("name"))])
    return None


def _run(workspace: object, job_id: object) -> subprocess.CompletedProcess[str]:
    root = accepted_workspace(workspace)
    job = accepted_job(job_id)
    args = [part for part in container_arguments(root, JOB_IMAGE, job_id=job) if part != "--rm"]
    return _docker(["run", "-d", "--name", container_name(job), *args[1:]])


def _logs(name: object) -> dict[str, str]:
    captured = _bounded_logs(accepted_name(name), limit=LOG_CAPTURE_LIMIT)
    return {"stdout": captured["stdout"], "stderr": captured["stderr"]}


if __name__ == "__main__":
    main()
