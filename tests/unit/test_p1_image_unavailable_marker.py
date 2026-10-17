"""The runner and the worker agree on the image_unavailable stderr marker through one shared constant."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from aether_worker import coding_sandbox, openhands_image, runner_process, sandbox_delegate
from aether_worker.coding_sandbox import CodingSandboxError, run_coding_sandbox

OBJECTIVE = "Make value() return 42."
_SENTINEL = "marker-under-test"


def _delegated_job_without_image(monkeypatch: pytest.MonkeyPatch) -> BaseException | None:
    monkeypatch.setattr(openhands_image, "coding_runtime_image", lambda: None)
    monkeypatch.setattr(runner_process, "_docker", lambda *_a, **_k: pytest.fail("docker run without an image"))
    monkeypatch.setattr(coding_sandbox, "_docker", lambda *_a, **_k: pytest.fail("docker run without an image"))
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: [])
    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", lambda *_a, **_k: pytest.fail("waited for a container"))
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")

    def ask(op: str, fields: dict, *, timeout: float | None) -> dict:
        if op != "run":
            raise sandbox_delegate.SandboxDelegateError("unexpected op")
        status, body = runner_process._sandbox({"op": op, **fields})
        assert status == 200, body
        return body

    monkeypatch.setattr(sandbox_delegate, "_ask", ask)
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    try:
        run_coding_sandbox(workspace, job_id="job-marker-1", proxy_dir=proxy, job_token="job-token-unit", objective=OBJECTIVE)
    except BaseException as exc:  # noqa: BLE001 - handed back to the test
        return exc
    finally:
        workspace.rmdir()
        proxy.rmdir()
    return None


def test_both_sides_import_one_shared_marker() -> None:
    assert openhands_image.IMAGE_UNAVAILABLE == "image_unavailable"
    assert runner_process.IMAGE_UNAVAILABLE is openhands_image.IMAGE_UNAVAILABLE
    assert coding_sandbox.IMAGE_UNAVAILABLE is openhands_image.IMAGE_UNAVAILABLE


def test_the_runner_writes_the_shared_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_process, "IMAGE_UNAVAILABLE", _SENTINEL)
    monkeypatch.setattr(openhands_image, "coding_runtime_image", lambda: None)
    monkeypatch.setattr(runner_process, "_docker", lambda *_a, **_k: pytest.fail("docker run without an image"))
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    try:
        launched = runner_process._run(workspace, "job-marker-2", proxy, "job-token-unit", OBJECTIVE)
    finally:
        workspace.rmdir()
        proxy.rmdir()
    assert launched.returncode != 0
    assert launched.stderr == _SENTINEL


def test_the_worker_recognises_the_shared_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_process, "IMAGE_UNAVAILABLE", _SENTINEL)
    monkeypatch.setattr(coding_sandbox, "IMAGE_UNAVAILABLE", _SENTINEL)
    error = _delegated_job_without_image(monkeypatch)
    assert isinstance(error, CodingSandboxError)
    assert error.code == "image_unavailable"


def test_a_marker_the_worker_does_not_share_is_not_read_as_image_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_process, "IMAGE_UNAVAILABLE", _SENTINEL)
    error = _delegated_job_without_image(monkeypatch)
    assert not (isinstance(error, CodingSandboxError) and error.code == "image_unavailable")
