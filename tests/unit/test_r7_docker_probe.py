"""Docker runner enables only after a daemon probe. A miss is unsupported."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from reference.models import ArtifactRef, CodingJobRequest

from aether_worker.runner import LinuxRunner, RunnerError, _docker, docker_available, probe_docker_daemon


def _request() -> CodingJobRequest:
    return CodingJobRequest(
        project_id="project-a",
        task_id="task-a",
        intent_id="intent-probe",
        base_artifact=ArtifactRef(
            artifact_id="artifact-base",
            project_id="project-a",
            sha256="a" * 64,
            byte_size=1,
            media_type="text/plain",
        ),
        objective="探测",
        contract_sha256="b" * 64,
        allowed_tools=["tool.read"],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_missing_cli_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    # cli_missing means the SDK could not reach a daemon either; do not depend on the host's socket.
    monkeypatch.setattr("aether_worker.runner._docker", lambda _args: subprocess.CompletedProcess(["docker"], 1, "", "no socket"))
    probed = probe_docker_daemon()
    assert probed["runtime"] == "unsupported"
    assert probed["reason"] == "cli_missing"
    assert docker_available() is False


def test_daemon_failure_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    def failed(_args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["docker"], 1, "", "cannot connect")

    monkeypatch.setattr("aether_worker.runner._docker", failed)
    probed = probe_docker_daemon()
    assert probed["runtime"] == "unsupported"
    assert probed["reason"] == "daemon_unreachable"


def test_server_version_enables_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    def version(_args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["docker"], 0, "29.1.3\n", "")

    monkeypatch.setattr("aether_worker.runner._docker", version)
    probed = probe_docker_daemon()
    assert probed == {"runtime": "enabled", "server_version": "29.1.3"}
    assert docker_available() is True


def test_worker_role_does_not_call_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")

    def forbidden(_args: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("worker called docker")

    monkeypatch.setattr("aether_worker.runner._docker", forbidden)
    probed = probe_docker_daemon()
    assert probed["runtime"] == "unsupported"
    assert probed["reason"] == "docker_forbidden"
    with pytest.raises(RunnerError) as raised:
        _docker(["info"])
    assert raised.value.code == "docker_forbidden"


def test_worker_coding_ps_asks_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    monkeypatch.setenv("AETHER_RUNNER_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "token")
    seen: dict[str, object] = {}

    def fake(url: str, token: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        seen["url"] = url
        seen["token"] = token
        seen["payload"] = payload
        seen["timeout"] = timeout
        return {"returncode": 0, "stdout": "abc\n", "stderr": ""}

    def forbidden(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("worker called docker")

    monkeypatch.setattr("aether_worker.coding_sandbox.docker_available", lambda: True)
    monkeypatch.setattr("aether_worker.sandbox_delegate.post_json", fake)
    monkeypatch.setattr("aether_worker.coding_sandbox._docker", forbidden)
    from aether_worker.coding_sandbox import coding_container_ids

    assert coding_container_ids("11111111-1111-1111-1111-111111111111") == ["abc"]
    assert seen["url"] == "http://127.0.0.1:9/v1/sandbox"
    assert seen["token"] == "token"
    assert seen["payload"] == {"op": "ps", "job_id": "11111111-1111-1111-1111-111111111111"}


def test_start_records_unsupported_without_a_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aether_worker.runner.probe_docker_daemon", lambda: {"runtime": "unsupported", "reason": "daemon_unreachable"})

    def launched(*_args, **_kwargs) -> str:
        raise AssertionError("unsupported probe still launched a container")

    monkeypatch.setattr("aether_worker.runner._launch_container", launched)
    result = LinuxRunner(tmp_path).start(_request(), token="token", gateway_url="http://127.0.0.1:9/complete", provider_key="")
    assert result.status == "ACTIVE"
    saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    record = next(iter(saved.values()))
    assert record["runtime"] == "unsupported"
    assert "container_id" not in record


def test_start_launches_when_the_probe_is_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aether_worker.runner.probe_docker_daemon", lambda: {"runtime": "enabled", "server_version": "29.1.3"})
    monkeypatch.setattr("aether_worker.runner._container_id", lambda _key: None)
    monkeypatch.setattr("aether_worker.runner._launch_container", lambda *_args, **_kwargs: "cid-1")
    result = LinuxRunner(tmp_path).start(_request(), token="token", gateway_url="http://127.0.0.1:9/complete", provider_key="")
    assert result.status == "ACTIVE"
    saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    record = next(iter(saved.values()))
    assert record["runtime"] == "enabled"
    assert record["container_id"] == "cid-1"
