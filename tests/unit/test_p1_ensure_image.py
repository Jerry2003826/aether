"""ensure_image(client, tag, *, allow_pull, allow_build): local, else pull, else build, else ImageUnavailable."""

from __future__ import annotations

from typing import Any

import pytest
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from aether_worker import openhands_image
from aether_worker.openhands_image import ImageUnavailable, ensure_image

_TAG = "aether-openhands-sandbox:1.54.0-abc"
_OTHER = "alpine@sha256:0123"


class _Image:
    def __init__(self, image_id: str) -> None:
        self.id = image_id


class _Images:
    def __init__(self, calls: list[str], *, local: Any = None, pulled: Any = None) -> None:
        self.calls = calls
        self.local = local
        self.pulled = pulled

    def get(self, tag: str) -> _Image:
        self.calls.append(f"get {tag}")
        if isinstance(self.local, BaseException):
            raise self.local
        if self.local is None:
            raise ImageNotFound(f"no such image: {tag}")
        return _Image(self.local)

    def pull(self, tag: str, **_kwargs: Any) -> _Image:
        self.calls.append(f"pull {tag}")
        if isinstance(self.pulled, BaseException):
            raise self.pulled
        if self.pulled is None:
            raise ImageNotFound(f"pull: {tag} not found")
        return _Image(self.pulled)


class _Client:
    def __init__(self, **kwargs: Any) -> None:
        self.calls: list[str] = []
        self.images = _Images(self.calls, **kwargs)


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fake client; the sandbox build is recorded on the same call log."""

    def make(*, build: Any = "sha256:built", **kwargs: Any) -> _Client:
        client = _Client(**kwargs)

        def built() -> str:
            client.calls.append("build")
            if isinstance(build, BaseException):
                raise build
            return build

        monkeypatch.setattr(openhands_image, "sandbox_tag", lambda root=None: _TAG)
        monkeypatch.setattr(openhands_image, "build_openhands_sandbox", built)
        return client

    return make


@pytest.mark.parametrize("allow_pull", [True, False])
@pytest.mark.parametrize("allow_build", [True, False])
def test_a_local_image_is_used_without_pull_or_build(client_for: Any, allow_pull: bool, allow_build: bool) -> None:
    client = client_for(local="sha256:local")
    assert ensure_image(client, _TAG, allow_pull=allow_pull, allow_build=allow_build) == "sha256:local"
    assert client.calls == [f"get {_TAG}"]


@pytest.mark.parametrize("allow_build", [True, False])
def test_a_missing_image_is_pulled_before_any_build(client_for: Any, allow_build: bool) -> None:
    client = client_for(pulled="sha256:pulled")
    assert ensure_image(client, _TAG, allow_build=allow_build) == "sha256:pulled"
    assert client.calls == [f"get {_TAG}", f"pull {_TAG}"]


def test_the_defaults_allow_pull_and_build(client_for: Any) -> None:
    client = client_for()
    assert ensure_image(client, _TAG) == "sha256:built"
    assert client.calls == [f"get {_TAG}", f"pull {_TAG}", "build"]


@pytest.mark.parametrize(
    "failure",
    [
        ImageNotFound("manifest unknown"),
        NotFound("repository does not exist"),
        APIError("pull access denied"),
        DockerException("registry unreachable"),
    ],
)
def test_a_failed_pull_falls_back_to_a_build(client_for: Any, failure: BaseException) -> None:
    client = client_for(pulled=failure)
    assert ensure_image(client, _TAG, allow_pull=True, allow_build=True) == "sha256:built"
    assert client.calls == [f"get {_TAG}", f"pull {_TAG}", "build"]


def test_a_failed_pull_without_build_is_image_pull_failed(client_for: Any) -> None:
    client = client_for(pulled=APIError("pull access denied"))
    with pytest.raises(ImageUnavailable) as caught:
        ensure_image(client, _OTHER, allow_pull=True, allow_build=False)
    assert caught.value.reason == "image_pull_failed"
    assert caught.value.tag == _OTHER
    assert client.calls == [f"get {_OTHER}", f"pull {_OTHER}"]


def test_build_only_skips_the_pull(client_for: Any) -> None:
    client = client_for()
    assert ensure_image(client, _TAG, allow_pull=False, allow_build=True) == "sha256:built"
    assert client.calls == [f"get {_TAG}", "build"]


def test_neither_pull_nor_build_is_image_unavailable(client_for: Any) -> None:
    client = client_for()
    with pytest.raises(ImageUnavailable) as caught:
        ensure_image(client, _TAG, allow_pull=False, allow_build=False)
    assert caught.value.reason == "image_unavailable"
    assert client.calls == [f"get {_TAG}"]


def test_a_failed_build_is_image_build_failed(client_for: Any) -> None:
    client = client_for(build=DockerException("build failed"))
    with pytest.raises(ImageUnavailable) as caught:
        ensure_image(client, _TAG)
    assert caught.value.reason == "image_build_failed"
    assert client.calls == [f"get {_TAG}", f"pull {_TAG}", "build"]


def test_only_the_openhands_sandbox_tag_can_be_built(client_for: Any) -> None:
    client = client_for()
    with pytest.raises(ImageUnavailable) as caught:
        ensure_image(client, _OTHER, allow_pull=True, allow_build=True)
    assert caught.value.reason == "image_build_unsupported"
    assert client.calls == [f"get {_OTHER}", f"pull {_OTHER}"]


def test_a_daemon_error_on_lookup_is_not_mistaken_for_a_missing_image(client_for: Any) -> None:
    client = client_for(local=APIError("daemon gone"))
    with pytest.raises(APIError):
        ensure_image(client, _TAG)
    assert client.calls == [f"get {_TAG}"]


def test_image_unavailable_is_a_lookup_error_and_not_a_docker_error() -> None:
    error = ImageUnavailable("image_pull_failed", _OTHER)
    assert isinstance(error, LookupError)
    assert not isinstance(error, DockerException)
    assert str(error) == "image_pull_failed"


def test_the_openhands_sandbox_is_ensured_by_its_hashed_tag(client_for: Any) -> None:
    client = client_for(build="sha256:sandbox")
    assert openhands_image.ensure_openhands_sandbox(client) == "sha256:sandbox"
    assert client.calls == [f"get {_TAG}", f"pull {_TAG}", "build"]
