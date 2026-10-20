"""Runner process. Docker control stays here, not in the worker process."""

from __future__ import annotations

import os
import subprocess
import sys

from docker.errors import DockerException

from aether_worker.coding_sandbox import (
    LOG_CAPTURE_LIMIT,
    RESULT_CAPTURE_LIMIT,
    _bounded_logs,
    container_arguments,
    container_name,
    sweep_leftover_coding_containers,
)
from aether_worker.net_guard import fixtures_enabled
from aether_worker.openhands_image import (
    IMAGE_UNAVAILABLE,
    ImageUnavailable,
    build_openhands_sandbox,
    coding_runtime_image,
    ensure_openhands_sandbox,
    launch_image,
)
from aether_worker.role_http import serve_role
from aether_worker.runner import JOB_IMAGE, _docker, probe_docker_daemon
from aether_worker.threat_launch import ThreatLaunchError, docker_cleanup, docker_runner, execute_container
from aether_worker.sandbox_delegate import (
    SandboxDelegateError,
    accepted_grace,
    accepted_job,
    accepted_name,
    accepted_objective,
    accepted_proxy,
    accepted_token,
    accepted_workspace,
)


def main() -> None:
    token = os.environ.pop("AETHER_LOOPBACK_TOKEN", "")
    if not token:
        raise SystemExit("runner 缺少 AETHER_LOOPBACK_TOKEN")
    os.environ["AETHER_PROCESS_ROLE"] = "runner"
    # The image is ensured by prepare_image before this role starts (role_launch), so the
    # worker's readiness wait never includes a first build on a clean host.
    started = probe_docker_daemon()
    # Coding containers a previous run left behind (crash, unconfirmed stop, failed removal). Here,
    # in the role that owns Docker and holds no verifier or provider secret, before any request.
    sweep_leftover_coding_containers()

    def health() -> dict[str, str]:
        return {"role": "runner", **started}

    def post(path: str, body: dict) -> tuple[int, dict]:
        if path == "/v1/probe":
            return 200, probe_docker_daemon()
        if path == "/v1/sandbox":
            return _sandbox(body)
        if path == "/v1/threat-browser":
            return _threat_browser(body)
        return 404, {"error": "not_found"}

    serve_role(token, health, post)


def _threat_browser(body: dict) -> tuple[int, dict]:
    payload = body.get("payload")
    browser_image = body.get("browser_image")
    tor_image = body.get("tor_image")
    if not isinstance(payload, dict) or not isinstance(browser_image, str) or not isinstance(tor_image, str):
        return 400, {"error": "rejected"}
    allowed = {"url", "browser_enabled", "tor_enabled", "onion_enabled", "owner_confirmed", "allow_hosts", "proxy"}
    if any(key not in allowed for key in payload):
        return 400, {"error": "rejected"}
    if browser_image != os.environ.get("AETHER_THREAT_BROWSER_IMAGE", "") or tor_image != os.environ.get("AETHER_TOR_IMAGE", ""):
        return 200, {"error": "image_unpinned"}
    fixture = body.get("fixture") is True
    if fixture and not fixtures_enabled():
        return 200, {"error": "fixture_hosts_rejected"}
    try:
        result = execute_container(
            payload,
            runner=docker_runner,
            cleanup=docker_cleanup,
            browser_image=browser_image,
            tor_image=tor_image,
            fixture=fixture,
        )
    except ThreatLaunchError as exc:
        return 200, {"error": exc.code}
    return 200, result


def _sandbox(body: dict) -> tuple[int, dict]:
    op = body.get("op")
    try:
        if op == "logs":
            return 200, _logs(body.get("name"), body.get("limit"))
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
        return _run(
            body.get("workspace"), body.get("job_id"), body.get("proxy_dir"), body.get("job_token"), body.get("objective")
        )
    if op == "stop":
        return _docker(["stop", "-t", accepted_grace(body.get("grace")), accepted_name(body.get("name"))])
    if op == "rm":
        return _docker(["rm", "-f", accepted_name(body.get("name"))])
    if op == "inspect_status":
        return _docker(["inspect", "-f", "{{.State.Status}}", accepted_name(body.get("name"))])
    if op == "inspect_exit":
        return _docker(["inspect", "-f", "{{.State.ExitCode}}", accepted_name(body.get("name"))])
    return None


def _ensure_coding_image(started: dict[str, str]) -> None:
    """Label check and local build only, no pull. Not on the startup path: prepare_image is.

    Kept for callers that already hold a probe result and must not reach a registry.
    """
    if started.get("runtime") != "enabled":
        return
    try:
        if coding_runtime_image() is not None:
            return
        build_openhands_sandbox()
    except (DockerException, OSError):
        sys.stderr.write("openhands sandbox image build failed\n")


def prepare_image() -> int:
    """Ensure the OpenHands sandbox image (local, else pull, else build). Exit status for the one-shot step.

    No Docker means nothing to prepare (0). A failed pull and build is reported (1); the
    roles still start and a coding job then fails as image_unavailable.
    """
    started = probe_docker_daemon()
    if started.get("runtime") != "enabled":
        return 0
    try:
        ensure_openhands_sandbox()
    except ImageUnavailable as exc:
        sys.stderr.write(f"openhands sandbox image unavailable: {exc.reason}\n")
        return 1
    except (DockerException, OSError):
        sys.stderr.write("openhands sandbox image build failed\n")
        return 1
    return 0


def _run(
    workspace: object, job_id: object, proxy_dir: object, job_token: object, objective: object
) -> subprocess.CompletedProcess[str]:
    root = accepted_workspace(workspace)
    job = accepted_job(job_id)
    proxy = accepted_proxy(proxy_dir)
    token = accepted_token(job_token)
    text = accepted_objective(objective)
    try:
        image = launch_image(JOB_IMAGE, JOB_IMAGE)
    except LookupError:
        return subprocess.CompletedProcess(["docker"], 1, "", IMAGE_UNAVAILABLE)
    args = [part for part in container_arguments(root, image, objective=text, job_id=job, proxy_dir=proxy, job_token=token) if part != "--rm"]
    return _docker(["run", "-d", "--name", container_name(job), *args[1:]])


def _logs(name: object, limit: object = None) -> dict[str, str]:
    """The worker may ask for up to RESULT_CAPTURE_LIMIT bytes so a large RESULT line stays whole."""
    wanted = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else LOG_CAPTURE_LIMIT
    captured = _bounded_logs(accepted_name(name), limit=min(wanted, RESULT_CAPTURE_LIMIT))
    return {"stdout": captured["stdout"], "stderr": captured["stderr"]}


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare-image"]:
        os.environ["AETHER_PROCESS_ROLE"] = "runner"
        raise SystemExit(prepare_image())
    main()
