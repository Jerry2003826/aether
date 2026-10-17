"""Container actions go through docker-py. The worker still cannot call them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import requests

from aether_worker.coding_sandbox import CODING_MEMORY_BYTES, container_arguments
from aether_worker.container_runtime import container_logs, execute_docker, set_docker_client
from aether_worker.runner import JOB_IMAGE


class _ImageApi:
    def __init__(self) -> None:
        self.pulled: tuple[str, str | None] = ("", None)

    def pull(self, repository: str, tag: str | None = None) -> None:
        self.pulled = (repository, tag)


class _Container:
    def __init__(self) -> None:
        self.id = "cid-1"
        self.killed = False
        self.removed = False

    def wait(self, **_kwargs: object) -> dict[str, int]:
        raise requests.exceptions.ReadTimeout("slow")

    def kill(self) -> None:
        self.killed = True

    def logs(self, **_kwargs: object) -> bytes:
        return b""

    def remove(self, **_kwargs: object) -> None:
        self.removed = True


class _Containers:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}
        self.container = _Container()

    def run(self, image: str, command: list[str] | None, **kwargs: object) -> _Container:
        self.kwargs = {"image": image, "command": command, **kwargs}
        return self.container


class _Client:
    def __init__(self) -> None:
        self.images = _ImageApi()
        self.containers = _Containers()

    def info(self) -> dict[str, str]:
        return {"ServerVersion": "29.1.3"}


def test_run_and_pull_use_the_sdk_and_not_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess docker")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    client = _Client()
    set_docker_client(client)
    try:
        version = execute_docker(["info", "--format", "{{.ServerVersion}}"])
        digest = "python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f"
        pulled = execute_docker(["pull", digest])
        args = container_arguments(tmp_path, "python:3.12-alpine")
        with pytest.raises(subprocess.TimeoutExpired):
            execute_docker(args, timeout=3)
    finally:
        set_docker_client(None)
    assert version.stdout == "29.1.3\n"
    assert pulled.returncode == 0
    assert client.images.pulled == (
        "python",
        "sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f",
    )
    kwargs = client.containers.kwargs
    assert kwargs["network_mode"] == "none"
    assert kwargs["read_only"] is True
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["pids_limit"] == 64
    assert kwargs["user"] == "65534:65534"
    assert kwargs["detach"] is True
    assert kwargs["mem_limit"] == CODING_MEMORY_BYTES
    assert kwargs["memswap_limit"] == CODING_MEMORY_BYTES
    assert client.containers.container.killed is True
    joined = " ".join(args)
    assert "/opt/aether/openhands_sandbox_entry.py" in joined
    assert "importlib" not in joined
    assert JOB_IMAGE.startswith("python@sha256:")


def test_the_sdk_receives_the_objective_unchanged(tmp_path: Path) -> None:
    objective = "say \"hi\"\nthen $(id) `id` ${HOME} {x} a=b=c 中文"
    client = _Client()
    set_docker_client(client)
    try:
        args = container_arguments(tmp_path, "python:3.12-alpine", objective=objective)
        execute_docker(["run", "-d", "--name", "aether-code-objective", *args[1:]])
    finally:
        set_docker_client(None)
    environment = client.containers.kwargs["environment"]
    assert isinstance(environment, dict)
    assert environment["AETHER_OBJECTIVE"] == objective


class _StreamContainer:
    def __init__(self) -> None:
        self.command: list[str] | None = None
        self.exec_kwargs: dict[str, object] = {}
        self.signal: str | None = None
        self.killed = False
        self.removed = False

    def exec_run(self, command: list[str], **kwargs: object) -> tuple[int, tuple[bytes, bytes]]:
        self.command = command
        self.exec_kwargs = kwargs
        return 0, (b'{"forbidden":[]}\n', b"")

    def kill(self, signal: str | None = None) -> None:
        self.killed = True
        self.signal = signal

    def remove(self, **_kwargs: object) -> None:
        self.removed = True

    def wait(self, **_kwargs: object) -> dict[str, int]:
        raise requests.exceptions.ReadTimeout("slow")

    def logs(self, **kwargs: object) -> object:
        assert kwargs.get("stream") is True
        assert kwargs.get("follow") is False
        if kwargs.get("stdout") is True and kwargs.get("stderr") is False:
            return iter([b"HEAD", b"Z" * 70000])
        if kwargs.get("stderr") is True and kwargs.get("stdout") is False:
            return iter([b"ERRHEAD", b"E" * 70000])
        raise AssertionError(kwargs)


class _StreamContainers:
    def __init__(self) -> None:
        self.container = _StreamContainer()
        self.kwargs: dict[str, object] = {}

    def get(self, name: str) -> _StreamContainer:
        assert name == "cid"
        return self.container

    def run(self, image: str, command: list[str] | None, **kwargs: object) -> _StreamContainer:
        self.kwargs = {"image": image, "command": command, **kwargs}
        return self.container


class _StreamClient:
    def __init__(self) -> None:
        self.containers = _StreamContainers()


def test_exec_kill_and_log_tail_use_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess docker")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    client = _StreamClient()
    set_docker_client(client)
    try:
        ran = execute_docker(
            [
                "exec",
                "-e",
                "AETHER_PROBE_INTERNET=1.1.1.1",
                "-e",
                "AETHER_PROBE_INTERNET_PORT=443",
                "cid",
                "python",
                "-c",
                "print(1)",
            ]
        )
        killed = execute_docker(["kill", "cid"])
        stdout, stderr = container_logs("cid", limit=65536)
        with pytest.raises(subprocess.TimeoutExpired):
            execute_docker(["run", "--rm", "python:3.12-alpine", "sleep", "5"], timeout=1)
    finally:
        set_docker_client(None)
    assert ran.returncode == 0
    assert ran.stdout.strip() == '{"forbidden":[]}'
    assert client.containers.container.command == ["python", "-c", "print(1)"]
    environment = client.containers.container.exec_kwargs["environment"]
    assert environment == {"AETHER_PROBE_INTERNET": "1.1.1.1", "AETHER_PROBE_INTERNET_PORT": "443"}
    assert killed.returncode == 0
    assert client.containers.container.killed is True
    assert client.containers.container.signal is None
    assert stdout == b"Z" * 65536
    assert stderr == b"E" * 65536
    assert b"HEAD" not in stdout
    assert client.containers.container.removed is True
