"""P2-07 · LinuxRunner.cancel 只改本地记录，容器继续运行（需要 Docker）。

不变量 INV-STOP-03：取消编码作业要停止进程树并撤销作业令牌，"已取消"不能只是一个标记。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aether_worker.runner import LinuxRunner, _container_id, _docker, docker_available, release_containers
from reference.models import ArtifactRef, CodingJobRequest


def test_cancel_stops_the_job_container(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required for the runner")
    request = CodingJobRequest(
        project_id="project-audit-cancel",
        task_id="task-audit-cancel",
        intent_id="intent-audit-cancel",
        base_artifact=ArtifactRef(artifact_id="artifact-a", project_id="project-audit-cancel", sha256="c" * 64, byte_size=1, media_type="text/plain"),
        objective="取消",
        contract_sha256="d" * 64,
        allowed_tools=["tool.read"],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    key = f"{request.project_id}:{request.task_id}:{request.intent_id}"
    release_containers(key)
    runner = LinuxRunner(tmp_path / "runner")
    started = runner.start(request, token="job-token-audit", gateway_url="http://127.0.0.1:8080/complete", provider_key="")
    try:
        cancelled = runner.cancel(request.project_id, started.job_id)
        container = _container_id(key)
        running = None
        if container:
            running = _docker(["inspect", "-f", "{{.State.Running}}", container]).stdout.strip()
    finally:
        release_containers(key)
    detail = f"cancel 返回 {cancelled.status}，作业容器 {container} 运行状态={running}"
    assert running != "true", detail
