"""ensure_image(client, tag, *, allow_pull, allow_build): local, else pull, else build, else ImageUnavailable."""

from __future__ import annotations

from typing import Any

import pytest
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from aether_worker import openhands_image
from aether_worker.openhands_image import ImageUnavailable, ensure_image

_TAG = "aether-openhands-sandbox:1.54.0-abc"
_OTHER = "alpine@sha256:0123"


_HASH = "abc"
_GOOD = {"aether.openhands_sandbox_hash": _HASH}


class _Image:
    def __init__(self, image_id: str, labels: dict[str, str] | None = None) -> None:
        self.id = image_id
        self.labels = dict(_GOOD if labels is None else labels)


def _image(spec: Any) -> _Image:
    """A str is an id with the expected sandbox label; (id, labels) sets the labels."""
    if isinstance(spec, tuple):
        return _Image(spec[0], spec[1])
    return _Image(spec)


class _Images:
    def __init__(self, calls: list[str], *, local: Any = None, pulled: Any = None) -> None:
        self.calls = calls
        self.local = local
        self.pulled = pulled
        self.built: dict[str, _Image] = {}

    def get(self, tag: str) -> _Image:
        self.calls.append(f"get {tag}")
        if tag in self.built:
            return self.built[tag]
        if isinstance(self.local, BaseException):
            raise self.local
        if self.local is None:
            raise ImageNotFound(f"no such image: {tag}")
        return _image(self.local)

    def pull(self, tag: str, **_kwargs: Any) -> _Image:
        self.calls.append(f"pull {tag}")
        if isinstance(self.pulled, BaseException):
            raise self.pulled
        if self.pulled is None:
            raise ImageNotFound(f"pull: {tag} not found")
        return _image(self.pulled)


class _Client:
    def __init__(self, **kwargs: Any) -> None:
        self.calls: list[str] = []
        self.images = _Images(self.calls, **kwargs)


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fake client; the sandbox build is recorded on the same call log and registers its image."""

    def make(*, build: Any = "sha256:built", built_labels: dict[str, str] | None = None, **kwargs: Any) -> _Client:
        client = _Client(**kwargs)

        def built() -> str:
            client.calls.append("build")
            if isinstance(build, BaseException):
                raise build
            client.images.built[build] = _Image(build, built_labels)
            return build

        monkeypatch.setattr(openhands_image, "context_hash", lambda root=None: _HASH)
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
    assert client.calls == [f"get {_TAG}", f"pull {_TAG}", "build", "get sha256:built"]


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
    assert client.calls == [f"get {_TAG}", f"pull {_TAG}", "build", "get sha256:built"]


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
    assert client.calls == [f"get {_TAG}", "build", "get sha256:built"]


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


def test_the_openhands_sandbox_is_ensured_by_its_hashed_tag_and_built_never_pulled(client_for: Any) -> None:
    client = client_for(build="sha256:sandbox")
    assert openhands_image.ensure_openhands_sandbox(client) == "sha256:sandbox"
    assert client.calls == [f"get {_TAG}", "build", "get sha256:sandbox"]


def test_the_openhands_sandbox_is_not_pulled_even_when_a_registry_has_the_tag(client_for: Any) -> None:
    client = client_for(pulled="sha256:from-registry")
    assert openhands_image.ensure_openhands_sandbox(client) == "sha256:built"
    assert not any(call.startswith("pull ") for call in client.calls)


def test_the_openhands_sandbox_without_build_is_unavailable_and_not_pulled(client_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_for(pulled="sha256:from-registry", build=DockerException("no build"))
    with pytest.raises(ImageUnavailable) as caught:
        openhands_image.ensure_openhands_sandbox(client)
    assert caught.value.reason == "image_build_failed"
    assert not any(call.startswith("pull ") for call in client.calls)


@pytest.mark.parametrize("labels", [{}, {"aether.openhands_sandbox_hash": "stale"}])
def test_a_local_sandbox_image_without_the_expected_hash_label_is_rebuilt(client_for: Any, labels: dict[str, str]) -> None:
    client = client_for(local=("sha256:stale", labels))
    assert ensure_image(client, _TAG, allow_pull=False, allow_build=True) == "sha256:built"
    assert client.calls == [f"get {_TAG}", "build", "get sha256:built"]


@pytest.mark.parametrize("labels", [{}, {"aether.openhands_sandbox_hash": "stale"}])
def test_a_local_sandbox_image_without_the_expected_hash_and_no_build_is_unavailable(client_for: Any, labels: dict[str, str]) -> None:
    client = client_for(local=("sha256:stale", labels))
    with pytest.raises(ImageUnavailable) as caught:
        ensure_image(client, _TAG, allow_pull=False, allow_build=False)
    assert caught.value.reason == "image_unavailable"
    assert "build" not in client.calls


@pytest.mark.parametrize("labels", [{}, {"aether.openhands_sandbox_hash": "stale"}])
def test_a_pulled_sandbox_image_without_the_expected_hash_label_is_rejected(client_for: Any, labels: dict[str, str]) -> None:
    client = client_for(pulled=("sha256:foreign", labels))
    with pytest.raises(ImageUnavailable) as caught:
        ensure_image(client, _TAG, allow_pull=True, allow_build=False)
    assert caught.value.reason == "image_pull_failed"
    client = client_for(pulled=("sha256:foreign", labels))
    assert ensure_image(client, _TAG, allow_pull=True, allow_build=True) == "sha256:built"


def test_a_built_sandbox_image_without_the_expected_hash_label_is_rejected(client_for: Any) -> None:
    client = client_for(built_labels={"aether.openhands_sandbox_hash": "stale"})
    with pytest.raises(ImageUnavailable) as caught:
        ensure_image(client, _TAG, allow_pull=False, allow_build=True)
    assert caught.value.reason == "image_label_mismatch"


def test_other_tags_are_not_label_checked(client_for: Any) -> None:
    client = client_for(local=("sha256:alpine", {}))
    assert ensure_image(client, _OTHER) == "sha256:alpine"
    client = client_for(pulled=("sha256:alpine", {}))
    assert ensure_image(client, _OTHER, allow_build=False) == "sha256:alpine"
