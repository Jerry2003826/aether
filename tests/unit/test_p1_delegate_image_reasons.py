"""Every image-prep failure marker the runner's container runtime can answer a delegated run with is image_unavailable.

container_runtime._ensure_run_image fails a run whose image could not be made present with the
ImageUnavailable reason as stderr: image_unavailable, image_pull_failed or image_label_mismatch.
The worker's delegate path must read all three as image_unavailable and keep the precise reason.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from aether_worker import coding_sandbox, openhands_image, sandbox_delegate
from aether_worker.coding_sandbox import CodingSandboxError, run_coding_sandbox

OBJECTIVE = "Make value() return 42."
_MARKERS = ("image_unavailable", "image_pull_failed", "image_label_mismatch")


def _delegated_run_answered_with(monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str) -> BaseException | None:
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    monkeypatch.setattr(coding_sandbox, "_docker", lambda *_a, **_k: pytest.fail("local docker run on the delegate path"))
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: [])
    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", lambda *_a, **_k: pytest.fail("waited for a container"))

    def ask(op: str, fields: dict, *, timeout: float | None) -> dict:
        assert op == "run"
        return {"returncode": returncode, "stdout": "", "stderr": stderr}

    monkeypatch.setattr(sandbox_delegate, "_ask", ask)
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    try:
        run_coding_sandbox(workspace, job_id="job-reason-1", proxy_dir=proxy, job_token="job-token-unit", objective=OBJECTIVE)
    except BaseException as exc:  # noqa: BLE001 - handed back to the test
        return exc
    finally:
        workspace.rmdir()
        proxy.rmdir()
    return None


def test_the_markers_are_shared_constants_with_the_image_unavailable_reasons() -> None:
    assert openhands_image.IMAGE_PULL_FAILED == "image_pull_failed"
    assert openhands_image.IMAGE_LABEL_MISMATCH == "image_label_mismatch"
    assert openhands_image.RUN_IMAGE_REASONS == frozenset(
        {openhands_image.IMAGE_UNAVAILABLE, openhands_image.IMAGE_PULL_FAILED, openhands_image.IMAGE_LABEL_MISMATCH}
    )
    assert coding_sandbox.RUN_IMAGE_REASONS is openhands_image.RUN_IMAGE_REASONS


@pytest.mark.parametrize("marker", _MARKERS)
def test_each_image_marker_on_the_delegate_path_is_image_unavailable(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    error = _delegated_run_answered_with(monkeypatch, 1, marker)
    assert isinstance(error, CodingSandboxError)
    assert error.code == "image_unavailable"
    assert error.payload.get("reason") == marker


@pytest.mark.parametrize("marker", _MARKERS)
def test_a_marker_with_a_trailing_newline_is_still_recognised(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    error = _delegated_run_answered_with(monkeypatch, 1, f"{marker}\n")
    assert isinstance(error, CodingSandboxError)
    assert error.code == "image_unavailable"
    assert error.payload.get("reason") == marker


@pytest.mark.parametrize(
    "stderr",
    ["docker: Error response from daemon: boom", "image_pull_failed: extra", "image_build_failed", "image_build_unsupported", ""],
)
def test_any_other_failed_stderr_is_still_sandbox_failed(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    error = _delegated_run_answered_with(monkeypatch, 1, stderr)
    assert isinstance(error, CodingSandboxError)
    assert error.code == "sandbox_failed"


@pytest.mark.parametrize("marker", _MARKERS)
def test_a_marker_on_a_successful_run_is_not_an_image_failure(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    started: list[str] = []
    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", lambda job, **_k: started.append(job) or {"ok": True})
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: [])
    monkeypatch.setattr(
        sandbox_delegate, "_ask", lambda op, fields, *, timeout: {"returncode": 0, "stdout": "cid", "stderr": marker}
    )
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    try:
        result = run_coding_sandbox(workspace, job_id="job-reason-2", proxy_dir=proxy, job_token="job-token-unit", objective=OBJECTIVE)
    finally:
        workspace.rmdir()
        proxy.rmdir()
    assert result == {"ok": True}
    assert started == ["job-reason-2"]
