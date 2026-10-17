"""A clean host has no images: a pinned image the harness needs is pulled, a local build is not."""

from __future__ import annotations

import pytest
from docker.errors import APIError, ImageNotFound

from aether_worker.container_runtime import execute_docker, set_docker_client
from aether_worker.live_harness import ISOLATION_IMAGE, _linux_isolation

_SECRET = "sk-live-NOT-A-REAL-KEY"


class _Container:
    id = "cid-iso"

    def wait(self, **_kwargs: object) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool = True, stderr: bool = False, **_kwargs: object) -> bytes:
        return b"key False\nnet 101\n" if stdout else b""

    def remove(self, **_kwargs: object) -> None:
        return None


class _Image:
    def __init__(self, reference: str) -> None:
        self.id = "sha256:" + reference.encode().hex()[:12]


class _Images:
    def __init__(self, client: _Client, *, pull_fails: bool) -> None:
        self._client = client
        self._pull_fails = pull_fails
        self.pulls: list[tuple[str, str | None]] = []

    def get(self, reference: str) -> object:
        if reference not in self._client.present:
            raise ImageNotFound(f"No such image: {reference}")
        return _Image(reference)

    def pull(self, repository: str, tag: str | None = None, **_kwargs: object) -> object:
        # Like docker-py: a digest or tag may come inside the repository argument.
        if tag is None and "@" in repository:
            repository, tag = repository.split("@", 1)
        self.pulls.append((repository, tag))
        if self._pull_fails:
            raise APIError("pull access denied")
        reference = f"{repository}@{tag}" if tag and tag.startswith("sha256:") else f"{repository}:{tag}"
        self._client.present.add(reference)
        return _Image(reference)


class _Containers:
    def __init__(self, client: _Client) -> None:
        self._client = client
        self.ran: list[str] = []

    def run(self, image: str, command: object, **_kwargs: object) -> _Container:
        # Like a client that does not pull on its own: a missing image is ImageNotFound.
        if image not in self._client.present:
            raise ImageNotFound(f"No such image: {image}")
        self.ran.append(image)
        return _Container()


class _Client:
    def __init__(self, *, pull_fails: bool = False) -> None:
        self.present: set[str] = set()
        self.images = _Images(self, pull_fails=pull_fails)
        self.containers = _Containers(self)

    def info(self) -> dict[str, str]:
        return {"ServerVersion": "29.1.3"}


@pytest.fixture
def docker_host(monkeypatch):
    monkeypatch.setattr("aether_worker.live_harness.shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("aether_worker.live_harness.sys.platform", "linux")
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)

    def install(client: _Client) -> _Client:
        set_docker_client(client)
        return client

    yield install
    set_docker_client(None)


def test_missing_pinned_image_is_pulled_then_the_isolation_container_runs(docker_host) -> None:
    client = docker_host(_Client())
    found = _linux_isolation(_SECRET)
    repository, digest = ISOLATION_IMAGE.split("@", 1)
    assert client.images.pulls == [(repository, digest)]
    assert client.containers.ran == [ISOLATION_IMAGE]
    assert found["status"] == "pass", found
    assert _SECRET not in repr(found)


def test_a_failed_pull_is_a_clear_not_run(docker_host) -> None:
    client = docker_host(_Client(pull_fails=True))
    found = _linux_isolation(_SECRET)
    assert len(client.images.pulls) == 1
    assert client.containers.ran == []
    assert found["status"] == "not_run", found
    assert found["reason"] == "image_pull_failed"


def test_a_missing_local_build_is_not_pulled(docker_host) -> None:
    client = docker_host(_Client())
    completed = execute_docker(["run", "--rm", "aether-openhands-sandbox:1.54.0-0123456789abcdef", "python", "-c", "1"])
    assert client.images.pulls == []
    assert completed.returncode != 0
    assert completed.stderr.startswith("image_unavailable")
