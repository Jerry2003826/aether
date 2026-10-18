"""The startup sweep runs in the runner, where every docker argv goes through docker-py
(container_runtime). There the inspect template the sweep asks for is rendered from the container's
own attributes, and a template the SDK path cannot render is an error, never an empty answer."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest
from aether_worker import coding_sandbox
from aether_worker.container_runtime import execute_docker, set_docker_client
from docker.errors import NotFound

_CREATED = "2026-10-18T09:00:00.123456789Z"


def _state(status: str) -> dict[str, Any]:
    return {
        "Status": status,
        "Running": status == "running",
        "ExitCode": 0,
        "StartedAt": "2026-10-18T09:00:01.5Z",
        "FinishedAt": "2026-10-18T09:05:00.25Z" if status in {"exited", "dead"} else "0001-01-01T00:00:00Z",
    }


class _Container:
    def __init__(self, client: _Client, cid: str, status: str, labels: dict[str, str]) -> None:
        self.client = client
        self.id = cid
        self.labels = labels
        self.attrs: dict[str, Any] = {"Id": cid, "Created": _CREATED, "State": _state(status), "Config": {"Labels": labels, "Image": "python:3.12-alpine"}}

    def reload(self) -> None:
        return None

    def remove(self, *, force: bool = False) -> None:
        self.client.removed.append(self.id)
        self.client.containers.items.pop(self.id, None)


class _Containers:
    def __init__(self, client: _Client) -> None:
        self.client = client
        self.items: dict[str, _Container] = {}
        self.filters: list[dict[str, Any]] = []

    def get(self, name: str) -> _Container:
        if name not in self.items:
            raise NotFound("no such container")
        return self.items[name]

    def list(self, **options: Any) -> list[_Container]:
        """Docker's own filter semantics: every label must match, any one status may."""
        assert options.get("all") is True
        filters = options.get("filters") or {}
        self.filters.append(filters)
        labels = filters.get("label", [])
        statuses = filters.get("status", [])
        found = []
        for item in self.items.values():
            pairs = {f"{key}={value}" for key, value in item.labels.items()}
            if not all(label in pairs or label in item.labels for label in labels):
                continue
            if statuses and item.attrs["State"]["Status"] not in statuses:
                continue
            found.append(item)
        return found


class _Images:
    def get(self, reference: str) -> object:
        class Image:
            id = "sha256:fixture-image-id"

        return Image()


class _Client:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.containers = _Containers(self)
        self.images = _Images()

    def add(self, cid: str, status: str, labels: dict[str, str]) -> None:
        self.containers.items[cid] = _Container(self, cid, status, labels)


@pytest.fixture
def client() -> Any:
    fake = _Client()
    set_docker_client(fake)
    yield fake
    set_docker_client(None)


def _sweep_inspect_argv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The exact inspect argv the sweep sends for a listed container."""
    sent: list[list[str]] = []

    def docker(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        sent.append(list(args))
        if args[:2] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(args, 0, "aether-code-listed\n", "")
        return subprocess.CompletedProcess(args, 1, "", "stop here")

    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", docker)
    coding_sandbox.sweep_leftover_coding_containers()
    inspects = [call for call in sent if call[:1] == ["inspect"]]
    assert len(inspects) == 1
    return inspects[0]


def test_the_sweeps_inspect_template_is_rendered_from_the_container(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    argv = _sweep_inspect_argv(monkeypatch)
    client.add("aether-code-listed", "exited", {"aether.role": "coding"})
    completed = execute_docker(argv)
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"State": _state("exited"), "Created": _CREATED}


def test_an_inspect_template_the_sdk_path_cannot_render_is_an_error(client: _Client) -> None:
    client.add("aether-code-listed", "exited", {"aether.role": "coding"})
    completed = execute_docker(["inspect", "-f", "{{.Config.Image}}", "aether-code-listed"])
    assert completed.returncode != 0
    assert completed.stdout == ""


def test_an_image_template_the_sdk_path_cannot_render_is_an_error(client: _Client) -> None:
    completed = execute_docker(["image", "inspect", "--format", "{{.Size}}", "python:3.12-alpine"])
    assert completed.returncode != 0
    assert completed.stdout == ""


def test_the_templates_the_sdk_path_renders_still_answer(client: _Client) -> None:
    client.add("aether-code-listed", "exited", {"aether.role": "coding"})
    assert execute_docker(["inspect", "-f", "{{.State.Status}}", "aether-code-listed"]).stdout == "exited\n"
    assert execute_docker(["inspect", "-f", "{{.State.ExitCode}}", "aether-code-listed"]).stdout == "0\n"
    assert execute_docker(["inspect", "-f", "{{.State.Running}}", "aether-code-listed"]).stdout == "false\n"
    assert execute_docker(["image", "inspect", "--format", "{{.Id}}", "python:3.12-alpine"]).stdout == "sha256:fixture-image-id\n"
    assert execute_docker(["image", "inspect", "python:3.12-alpine"]).returncode == 0
    assert execute_docker(["inspect", "aether-code-listed"]).returncode == 0


def test_ps_passes_status_filters_as_status_not_as_labels(client: _Client) -> None:
    client.add("aether-code-exited", "exited", {"aether.role": "coding"})
    client.add("aether-code-dead", "dead", {"aether.role": "coding"})
    client.add("aether-code-running", "running", {"aether.role": "coding"})
    completed = execute_docker(
        ["ps", "-aq", "--filter", "label=aether.role=coding", "--filter", "status=exited", "--filter", "status=dead"]
    )
    assert client.containers.filters == [{"label": ["aether.role=coding"], "status": ["exited", "dead"]}]
    assert completed.returncode == 0
    assert sorted(completed.stdout.split()) == ["aether-code-dead", "aether-code-exited"]


def test_ps_with_a_filter_the_sdk_path_does_not_translate_is_an_error(client: _Client) -> None:
    client.add("aether-code-exited", "exited", {"aether.role": "coding"})
    completed = execute_docker(["ps", "-aq", "--filter", "label=aether.role=coding", "--filter", "ancestor=python:3.12-alpine"])
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert client.containers.filters == []


def test_the_runner_sweep_through_the_sdk_removes_this_instances_old_exited_containers(
    monkeypatch: pytest.MonkeyPatch, client: _Client
) -> None:
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "runner")
    monkeypatch.setenv("AETHER_WORKER_INSTANCE_ID", "worker-seven")
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    mine = {"aether.role": "coding", "aether.worker-instance": "worker-seven"}
    client.add("aether-code-mine-exited", "exited", mine)
    client.add("aether-code-mine-dead", "dead", mine)
    client.add("aether-code-mine-running", "running", mine)
    client.add("aether-code-other-exited", "exited", {"aether.role": "coding", "aether.worker-instance": "another-instance"})
    later = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    removed = coding_sandbox.sweep_leftover_coding_containers(now=later)
    assert sorted(removed) == ["aether-code-mine-dead", "aether-code-mine-exited"]
    assert sorted(client.removed) == ["aether-code-mine-dead", "aether-code-mine-exited"]
    assert set(client.containers.items) == {"aether-code-mine-running", "aether-code-other-exited"}
