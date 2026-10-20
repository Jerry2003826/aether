"""An image lookup that answers 404 means the image is not here, whatever words the daemon uses.

docker-py raises ImageNotFound only when the 404 message holds one of a few known phrases; any other
404 (another daemon's wording, such as "image not known") is a plain NotFound. Each image lookup
treats both as missing: the threat browser's SDK run pulls a digest-pinned image as `docker run`
would, and ensure_image and the run-time image check pull instead of failing."""

from __future__ import annotations

from typing import Any

import pytest
import requests
from aether_worker import container_runtime, openhands_image, threat_launch
from docker.errors import NotFound, create_api_error_from_http_exception

_DIGEST_IMAGE = "registry.example/aether-threat-browser@sha256:" + "a" * 64


def _daemon_404(message: str) -> NotFound:
    """The exception docker-py itself raises for an HTTP 404 with this message."""
    response = requests.Response()
    response.status_code = 404
    response._content = ('{"message": "' + message + '"}').encode()
    response.url = "http+docker://localhost/v1.52/images/x/json"
    try:
        create_api_error_from_http_exception(requests.exceptions.HTTPError(response=response))
    except NotFound as exc:
        return exc
    raise AssertionError("docker-py did not raise NotFound")


def test_the_404_used_here_is_a_plain_not_found() -> None:
    error = _daemon_404("image not known")
    assert type(error) is NotFound


class _Api:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def inspect_image(self, image: str) -> dict[str, Any]:
        self.calls.append(f"inspect {image}")
        raise _daemon_404("image not known")

    def pull(self, image: str) -> None:
        self.calls.append(f"pull {image}")


def test_the_threat_browser_pulls_a_missing_digest_image_after_a_plain_404() -> None:
    api = _Api()
    threat_launch._sdk_image(api, _DIGEST_IMAGE)
    assert api.calls == [f"inspect {_DIGEST_IMAGE}", f"pull {_DIGEST_IMAGE}"]


def test_a_missing_unpinned_threat_image_is_image_unavailable_after_a_plain_404() -> None:
    api = _Api()
    with pytest.raises(threat_launch.ThreatLaunchError) as raised:
        threat_launch._sdk_image(api, "aether-threat-browser:local")
    assert raised.value.code == "image_unavailable"
    assert api.calls == ["inspect aether-threat-browser:local"]


class _Image:
    def __init__(self) -> None:
        self.id = "sha256:pulled"
        self.labels: dict[str, str] = {}


class _Images:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, tag: str) -> _Image:
        self.calls.append(f"get {tag}")
        raise _daemon_404("image not known")

    def pull(self, tag: str, **_kwargs: Any) -> _Image:
        self.calls.append(f"pull {tag}")
        return _Image()


class _Client:
    def __init__(self) -> None:
        self.images = _Images()


def test_ensure_image_pulls_after_a_plain_404_on_the_local_lookup() -> None:
    client = _Client()
    assert openhands_image.ensure_image(client, "python:3.12-alpine", allow_pull=True, allow_build=False) == "sha256:pulled"
    assert client.images.calls == ["get python:3.12-alpine", "pull python:3.12-alpine"]


def test_the_run_time_image_check_pulls_after_a_plain_404() -> None:
    client = _Client()
    container_runtime.set_docker_client(client)
    try:
        assert container_runtime._ensure_run_image("python:3.12-alpine") is None
    finally:
        container_runtime.set_docker_client(None)
    assert client.images.calls == ["get python:3.12-alpine", "get python:3.12-alpine", "pull python:3.12-alpine"]
