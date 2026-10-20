"""A host with the Docker SDK and a reachable daemon but no docker CLI still runs the Docker checks (Codex 10:25).

Every runner Docker call goes through docker-py (container_runtime.execute_docker), and
runner.probe_docker_daemon already enables the runtime from the SDK alone. live_harness._linux_isolation
and runner.probe_linux_isolation still returned early on `shutil.which("docker") is None`, so on such a
host the SDK-based isolation check was reported not_run (docker_missing) without ever being tried.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from aether_worker import live_harness, runner

_SECRET = "固定的测试密钥短语"


class _FakeSdk:
    """Stands in for container_runtime.execute_docker: a daemon the SDK reaches, no CLI involved."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if not self.reachable:
            return subprocess.CompletedProcess(["docker", *args], 1, "", "sdk_unreachable")
        if args[0] == "info":
            return subprocess.CompletedProcess(["docker", *args], 0, "29.0.0\n", "")
        if args[0] == "run":
            return subprocess.CompletedProcess(["docker", *args], 0, "key False\nnet 101\n", "")
        return subprocess.CompletedProcess(["docker", *args], 1, "", "unexpected")


@pytest.fixture
def no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: None if name == "docker" else "/usr/bin/" + name)


@pytest.mark.skipif(sys.platform != "linux", reason="the isolation check is linux only")
def test_the_live_isolation_check_runs_through_the_sdk_without_a_cli(no_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _FakeSdk()
    monkeypatch.setattr(runner, "execute_docker", sdk)
    found = live_harness._linux_isolation(_SECRET)
    assert any(call[0] == "run" for call in sdk.calls), (found, sdk.calls)
    assert found["status"] == "pass", found
    assert found["network_errno"] == 101


@pytest.mark.skipif(sys.platform != "linux", reason="the isolation check is linux only")
def test_no_cli_and_no_daemon_is_still_docker_missing(no_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _FakeSdk(reachable=False)
    monkeypatch.setattr(runner, "execute_docker", sdk)
    found = live_harness._linux_isolation(_SECRET)
    assert found == {"status": "not_run", "reason": "docker_missing"}
    assert not any(call[0] == "run" for call in sdk.calls)


def test_the_runner_isolation_probe_sees_the_sdk_daemon_without_a_cli(no_cli, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    sdk = _FakeSdk()
    monkeypatch.setattr(runner, "execute_docker", sdk)
    found = runner.probe_linux_isolation(tmp_path / "hide")
    assert any(call[0] == "info" for call in sdk.calls), sdk.calls
    assert found["docker"] == "present", found
