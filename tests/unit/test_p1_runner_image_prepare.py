"""The OpenHands image is ensured before the runner role starts, never inside its readiness wait."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from aether_worker import role_launch, runner_process


def test_runner_main_starts_serving_without_building_or_pulling(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-unit")
    monkeypatch.setattr(runner_process, "probe_docker_daemon", lambda: {"runtime": "enabled", "server_version": "27"})
    for name in ("build_openhands_sandbox", "coding_runtime_image", "ensure_openhands_sandbox"):
        if hasattr(runner_process, name):
            monkeypatch.setattr(runner_process, name, lambda *_a, _n=name, **_k: calls.append(_n))
    monkeypatch.setattr(runner_process, "sweep_leftover_coding_containers", list)
    monkeypatch.setattr(runner_process, "serve_role", lambda _token, _health, _post: calls.append("serve_role"))
    runner_process.main()
    assert calls == ["serve_role"]


def test_prepare_image_ensures_the_sandbox_when_docker_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    ensured: list[str] = []
    monkeypatch.setattr(runner_process, "probe_docker_daemon", lambda: {"runtime": "enabled", "server_version": "27"})
    monkeypatch.setattr(runner_process, "ensure_openhands_sandbox", lambda: ensured.append("ensure") or "sha256:x")
    assert runner_process.prepare_image() == 0
    assert ensured == ["ensure"]


def test_prepare_image_skips_without_docker_and_reports_a_failed_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from docker.errors import DockerException

    monkeypatch.setattr(runner_process, "probe_docker_daemon", lambda: {"runtime": "unsupported", "reason": "cli_missing"})
    monkeypatch.setattr(runner_process, "ensure_openhands_sandbox", lambda: pytest.fail("ensured without docker"))
    assert runner_process.prepare_image() == 0

    def broken() -> str:
        raise DockerException("build failed")

    monkeypatch.setattr(runner_process, "probe_docker_daemon", lambda: {"runtime": "enabled", "server_version": "27"})
    monkeypatch.setattr(runner_process, "ensure_openhands_sandbox", broken)
    assert runner_process.prepare_image() == 1


def _stub_launch(monkeypatch: pytest.MonkeyPatch, events: list[str], *, prepare: Any) -> None:
    for name in ("AETHER_WORKER_DATABASE_URL", "AETHER_VERIFIER_DATABASE_URL", "AETHER_DISPATCHER_DATABASE_URL", "AETHER_TEMPORAL_ADDRESS"):
        monkeypatch.setenv(name, f"postgresql://unit/{name.lower()}")
    monkeypatch.setattr(role_launch, "stop_recorded_roles", lambda: None)

    class _Proc:
        pid = 1

        def poll(self) -> None:
            return None

    def opened(module: str, *_args: Any) -> tuple[_Proc, list[str]]:
        events.append(f"open {module}")
        return _Proc(), []

    def awaited(_proc: Any, _errors: Any, _hide: Any) -> str:
        events.append("ready_wait")
        return "http://127.0.0.1:1"

    def ran(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("run " + " ".join(argv[1:]))
        assert argv[0] == sys.executable
        assert kwargs.get("env", {}).get("AETHER_PROCESS_ROLE") == "runner"
        assert "AETHER_LOOPBACK_TOKEN" not in kwargs.get("env", {})
        return prepare(argv, **kwargs)

    monkeypatch.setattr(role_launch, "_open", opened)
    monkeypatch.setattr(role_launch, "_await_role", awaited)
    monkeypatch.setattr(role_launch.subprocess, "run", ran)
    for name in ("scrub_privileged_env", "_record_role_pids", "_require_health", "_write_boundary", "_close_pipes"):
        monkeypatch.setattr(role_launch, name, lambda *_a, **_k: None)


def test_the_image_is_prepared_before_any_role_starts_or_any_readiness_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    _stub_launch(monkeypatch, events, prepare=lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "", ""))
    role_launch.launch_privileged_roles()
    assert events[0] == "run -m aether_worker.runner_process --prepare-image"
    assert events.count("run -m aether_worker.runner_process --prepare-image") == 1
    assert "open aether_worker.runner_process" in events
    assert events.index("open aether_worker.runner_process") > 0
    assert events.index("ready_wait") > 0


def test_a_failed_or_slow_prepare_does_not_stop_the_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    for outcome in ("failed", "timeout"):
        events: list[str] = []

        def prepare(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert kwargs.get("timeout") and kwargs["timeout"] > role_launch._READY_TIMEOUT
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 1, "", "openhands sandbox image build failed\n")

        _stub_launch(monkeypatch, events, prepare=prepare)
        role_launch.launch_privileged_roles()
        assert events[0].startswith("run ")
        assert "open aether_worker.runner_process" in events


def test_the_readiness_timeout_was_not_raised_to_cover_a_build() -> None:
    assert role_launch._READY_TIMEOUT == role_launch.ROLE_READY_SECONDS
