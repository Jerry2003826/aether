"""The startup sweep of leftover coding containers runs where Docker lives: in the runner role, which
holds no verifier or provider secret, before it starts serving. The worker process opens no Docker
client at any point of its start, neither before it hands the privileged credentials to their roles
nor after. The runner gets the worker's instance id, state directory and deployment id, so the
labels it puts on coding containers and the sweep it runs select the same instance."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from aether_worker import container_runtime, role_launch, runner_process, service
from aether_worker.goal_effect import WorkerEndpoints
from docker.errors import DockerException


class _Stop(Exception):
    pass


def _forbid_docker(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []

    def client() -> object:
        opened.append(os.environ.get("AETHER_PROCESS_ROLE", "unset"))
        raise DockerException("the worker process opened a docker client")

    monkeypatch.setattr(container_runtime, "docker_client", client)
    return opened


def _quiet_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(service, "warn_if_git_missing", lambda: None)
    monkeypatch.setattr(service, "warn_coding_rate_default", lambda: None)
    monkeypatch.setattr(service, "stop_children", lambda children: None)
    monkeypatch.setattr(service, "stop_recorded_roles", lambda: None)


def test_the_worker_opens_no_docker_client_before_the_credential_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _forbid_docker(monkeypatch)
    _quiet_start(monkeypatch)

    def handoff() -> None:
        raise _Stop

    monkeypatch.setattr(service, "_endpoints_for_process", handoff)
    with pytest.raises(_Stop):
        asyncio.run(service.serve())
    assert opened == []


def test_the_worker_opens_no_docker_client_when_it_is_given_its_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _forbid_docker(monkeypatch)
    _quiet_start(monkeypatch)
    monkeypatch.setattr(service, "configure", lambda endpoints: None)

    async def connect(*args: object, **kwargs: object) -> object:
        raise _Stop

    monkeypatch.setattr(service.Client, "connect", connect)
    endpoints = WorkerEndpoints(
        worker_url="",
        verifier_url="",
        dispatcher_url="",
        artifact_root=Path("/nonexistent"),
        temporal_address="127.0.0.1:1",
        task_queue="aether-test",
    )
    with pytest.raises(_Stop):
        asyncio.run(service.serve(endpoints))
    assert opened == []


def test_the_runner_sweeps_leftover_coding_containers_before_it_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    seen: dict[str, str | None] = {}
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-unit")
    monkeypatch.delenv("AETHER_LIVE_MODEL_KEY", raising=False)
    monkeypatch.delenv("AETHER_VERIFIER_DATABASE_URL", raising=False)
    monkeypatch.setattr(runner_process, "probe_docker_daemon", lambda: {"runtime": "enabled", "server_version": "27"})

    def sweep() -> list[str]:
        order.append("sweep")
        seen["role"] = os.environ.get("AETHER_PROCESS_ROLE")
        seen["token"] = os.environ.get("AETHER_LOOPBACK_TOKEN")
        return []

    monkeypatch.setattr(runner_process, "sweep_leftover_coding_containers", sweep, raising=False)
    monkeypatch.setattr(runner_process, "serve_role", lambda _token, _health, _post: order.append("serve"))
    runner_process.main()
    assert order == ["sweep", "serve"]
    # The runner role, with the loopback token already taken out of its environment.
    assert seen == {"role": "runner", "token": None}


def test_the_runner_gets_the_worker_instance_and_deployment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AETHER_WORKER_INSTANCE_ID", "worker-seven")
    monkeypatch.setenv("AETHER_WORKER_STATE_DIR", str(tmp_path / "worker-state"))
    monkeypatch.setenv("AETHER_DEPLOYMENT_ID", "stack-blue")
    monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", "fixture-provider-value")
    env = role_launch._runner_env("loopback-unit")
    assert env["AETHER_WORKER_INSTANCE_ID"] == "worker-seven"
    assert env["AETHER_WORKER_STATE_DIR"] == str(tmp_path / "worker-state")
    assert env["AETHER_DEPLOYMENT_ID"] == "stack-blue"
    assert "AETHER_LIVE_MODEL_KEY" not in env
    assert "AETHER_VERIFIER_DATABASE_URL" not in env


def test_the_runner_env_leaves_unset_identity_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AETHER_WORKER_INSTANCE_ID", "AETHER_WORKER_STATE_DIR", "AETHER_DEPLOYMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    env = role_launch._runner_env("loopback-unit")
    assert not {"AETHER_WORKER_INSTANCE_ID", "AETHER_WORKER_STATE_DIR", "AETHER_DEPLOYMENT_ID"} & set(env)
