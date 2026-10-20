"""Only a real timeout from container.wait is a deadline; a docker daemon or API error is a failure.

With a timeout set, docker-py raises requests.exceptions.ReadTimeout (or a ConnectionError wrapping
urllib3's ReadTimeoutError, which is how a read timeout on the unix socket surfaces) when the
container outlives it: that is subprocess.TimeoutExpired, which callers report as deadline_exceeded.
Anything else from wait (an APIError, the daemon gone, a refused connection) is a docker_error: a
failed result, not a deadline, and the container is still killed and removed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import requests
import urllib3
from aether_worker import verification
from aether_worker.container_runtime import execute_docker, set_docker_client
from docker.errors import APIError, DockerException


class _Images:
    def get(self, _reference: str) -> object:
        return object()


class _Container:
    def __init__(self, error: BaseException) -> None:
        self.id = "cid-wait"
        self.error = error
        self.killed = False
        self.removed = False

    def wait(self, **_kwargs: object) -> dict[str, int]:
        raise self.error

    def kill(self) -> None:
        self.killed = True

    def logs(self, **_kwargs: object) -> object:
        return iter([])

    def remove(self, **_kwargs: object) -> None:
        self.removed = True


class _Containers:
    def __init__(self, error: BaseException) -> None:
        self.container = _Container(error)

    def run(self, _image: str, _command: object, **_kwargs: object) -> _Container:
        return self.container

    def get(self, _name: str) -> _Container:
        # The grader removes its named container by name after a deadline.
        return self.container


class _Client:
    def __init__(self, error: BaseException) -> None:
        self.images = _Images()
        self.containers = _Containers(error)


def _run(error: BaseException, *, timeout: float | None = 5) -> tuple[_Container, subprocess.CompletedProcess[str] | subprocess.TimeoutExpired]:
    client = _Client(error)
    set_docker_client(client)
    try:
        try:
            outcome: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired = execute_docker(
                ["run", "--rm", "python:3.12-alpine", "sleep", "5"], timeout=timeout
            )
        except subprocess.TimeoutExpired as expired:
            outcome = expired
    finally:
        set_docker_client(None)
    return client.containers.container, outcome


def _wrapped_read_timeout() -> requests.exceptions.ConnectionError:
    inner = urllib3.exceptions.ReadTimeoutError(None, "/containers/cid-wait/wait", "Read timed out. (read timeout=5)")  # type: ignore[arg-type]
    return requests.exceptions.ConnectionError(inner)


_TIMEOUTS = {
    "read_timeout": lambda: requests.exceptions.ReadTimeout("Read timed out."),
    "unix_socket_read_timeout": _wrapped_read_timeout,
    "socket_timeout": lambda: TimeoutError("timed out"),
}

_FAILURES = {
    "api_error": lambda: APIError("500 Server Error: Internal Server Error"),
    "daemon_gone": lambda: requests.exceptions.ConnectionError("Connection aborted. FileNotFoundError(2, 'No such file or directory')"),
    "docker_exception": lambda: DockerException("Error while fetching server API version"),
    "chunked_encoding": lambda: requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"),
}


@pytest.mark.parametrize("name", sorted(_TIMEOUTS))
def test_only_a_read_timeout_from_wait_is_timeout_expired(name: str) -> None:
    container, outcome = _run(_TIMEOUTS[name]())
    assert isinstance(outcome, subprocess.TimeoutExpired), outcome
    assert container.killed is True
    assert container.removed is True


@pytest.mark.parametrize("name", sorted(_FAILURES))
def test_a_docker_api_error_from_wait_is_a_failure_not_a_deadline(name: str) -> None:
    container, outcome = _run(_FAILURES[name]())
    assert isinstance(outcome, subprocess.CompletedProcess), outcome
    assert outcome.returncode != 0
    assert outcome.stderr.startswith("docker_error")
    assert container.killed is True
    assert container.removed is True


def test_without_a_timeout_a_wait_error_is_still_a_failure() -> None:
    container, outcome = _run(APIError("500 Server Error"), timeout=None)
    assert isinstance(outcome, subprocess.CompletedProcess)
    assert outcome.returncode != 0
    assert outcome.stderr.startswith("docker_error")
    assert container.removed is True


def test_grader_container_api_error_is_not_deadline_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    client = _Client(APIError("500 Server Error: Internal Server Error"))
    set_docker_client(client)
    try:
        completed = verification.run_grader_container(tmp_path, timeout_seconds=30)
    except verification.VerificationError as exc:
        pytest.fail(f"an API error became {exc}")
    finally:
        set_docker_client(None)
    assert completed.returncode != 0
    assert completed.stderr.startswith("docker_error")


def test_grader_container_read_timeout_is_still_deadline_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    client = _Client(requests.exceptions.ReadTimeout("Read timed out."))
    set_docker_client(client)
    try:
        with pytest.raises(verification.VerificationError) as caught:
            verification.run_grader_container(tmp_path, timeout_seconds=30)
    finally:
        set_docker_client(None)
    assert caught.value.code == "deadline_exceeded"
