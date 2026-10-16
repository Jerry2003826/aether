"""P2-07: cancelling a coding job stops its containers and revokes its token. Docker is required.

INV-STOP-03. Ported from reports/audits/system_round1/repro/test_p2_07_runner_cancel.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aether_worker.runner import LinuxRunner, _docker, _job_label, docker_available, release_containers
from reference.models import ArtifactRef, CodingJobRequest

TOKEN = "job-token-audit"


def _request(intent: str) -> CodingJobRequest:
    return CodingJobRequest(
        project_id="project-audit-cancel",
        task_id="task-audit-cancel",
        intent_id=intent,
        base_artifact=ArtifactRef(artifact_id="artifact-a", project_id="project-audit-cancel", sha256="c" * 64, byte_size=1, media_type="text/plain"),
        objective="取消",
        contract_sha256="d" * 64,
        allowed_tools=["tool.read"],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def _containers(key: str) -> list[str]:
    label = _job_label(key)
    listed = _docker(["ps", "-aq", "--filter", f"label=aether.job={label}"])
    return [line.strip() for line in listed.stdout.splitlines() if line.strip()]


def test_cancel_stops_the_job_container(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required for the runner")
    request = _request("intent-audit-cancel")
    key = f"{request.project_id}:{request.task_id}:{request.intent_id}"
    release_containers(key)
    runner = LinuxRunner(tmp_path / "runner")
    started = runner.start(request, token=TOKEN, gateway_url="http://127.0.0.1:8080/complete", provider_key="")
    try:
        container = str(runner._find(request.project_id, started.job_id)["container_id"])
        cancelled = runner.cancel(request.project_id, started.job_id)
        inspected = _docker(["inspect", "-f", "{{.State.Running}}", container])
        running = inspected.stdout.strip() if inspected.returncode == 0 else "removed"
        remaining = _containers(key)
    finally:
        release_containers(key)
    detail = f"cancel 返回 {cancelled.status}，作业容器 {container} 运行状态={running}，剩余容器={remaining}"
    assert running != "true", detail
    assert remaining == [], detail
    assert cancelled.status == "CANCELLED", detail


def test_cancel_revokes_the_job_token_and_start_does_not_relaunch(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required for the runner")
    request = _request("intent-audit-revoke")
    key = f"{request.project_id}:{request.task_id}:{request.intent_id}"
    release_containers(key)
    runner = LinuxRunner(tmp_path / "runner")
    try:
        started = runner.start(request, token=TOKEN, gateway_url="http://127.0.0.1:8080/complete", provider_key="")
        runner.cancel(request.project_id, started.job_id)
        saved = (tmp_path / "runner" / "jobs.json").read_text(encoding="utf-8")
        record = json.loads(saved)[key]
        again = runner.start(request, token=TOKEN, gateway_url="http://127.0.0.1:8080/complete", provider_key="")
        restarted = LinuxRunner(tmp_path / "runner").start(request, token=TOKEN, gateway_url="http://127.0.0.1:8080/complete", provider_key="")
        remaining = _containers(key)
    finally:
        release_containers(key)
    assert TOKEN not in saved, record
    assert record["cancel_state"] == "confirmed", record
    assert (again.status, restarted.status) == ("CANCELLED", "CANCELLED")
    assert remaining == [], remaining
