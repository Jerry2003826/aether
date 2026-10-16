"""Container actions go through docker-py. The worker still cannot call them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import requests

from aether_worker.coding_sandbox import container_arguments
from aether_worker.container_runtime import execute_docker, set_docker_client
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

    def wait(self, **_kwargs: object) -> dict[str, int]:
        raise requests.exceptions.ReadTimeout("slow")

    def kill(self) -> None:
        self.killed = True

    def logs(self, **_kwargs: object) -> bytes:
        return b""

    def remove(self, **_kwargs: object) -> None:
        return None


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
    assert kwargs["mem_limit"] == 268435456
    assert client.containers.container.killed is True
    joined = " ".join(args)
    assert "/opt/aether/openhands_sandbox_entry.py" in joined
    assert "importlib" not in joined
    assert JOB_IMAGE.startswith("python@sha256:")
