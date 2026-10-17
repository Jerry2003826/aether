"""When image prep failed and the roles started anyway, a coding job fails at once as image_unavailable."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from aether_worker import coding_jobs, coding_sandbox, openhands_image, runner_process, sandbox_delegate
from aether_worker.coding_sandbox import CodingSandboxError, run_coding_sandbox

OBJECTIVE = "Make value() return 42."


def _no_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prep failed: no labeled sandbox image exists. Nothing may build, pull or start a container."""
    monkeypatch.setattr(openhands_image, "coding_runtime_image", lambda: None)
    monkeypatch.setattr(openhands_image, "build_openhands_sandbox", lambda: pytest.fail("built during a job"))
    monkeypatch.setattr(openhands_image, "ensure_openhands_sandbox", lambda *_a, **_k: pytest.fail("ensured during a job"))
    monkeypatch.setattr(runner_process, "_docker", lambda *_a, **_k: pytest.fail("docker run without an image"))
    monkeypatch.setattr(coding_sandbox, "_docker", lambda *_a, **_k: pytest.fail("docker run without an image"))
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: [])
    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", lambda *_a, **_k: pytest.fail("waited for a container"))


def _run_bounded(target) -> BaseException | None:
    """Run in a thread; a hang fails the test instead of the suite."""
    caught: dict[str, BaseException] = {}
    done = threading.Event()

    def run() -> None:
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            caught["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(10), "the job did not fail promptly"
    return caught.get("error")


def test_a_delegated_job_fails_promptly_as_image_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_image(monkeypatch)
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    asked: list[str] = []

    def ask(op: str, fields: dict, *, timeout: float | None) -> dict:
        asked.append(op)
        status, body = runner_process._sandbox({"op": op, **fields})
        assert status == 200, body
        return body

    monkeypatch.setattr(sandbox_delegate, "_ask", ask)
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    try:
        error = _run_bounded(
            lambda: run_coding_sandbox(workspace, job_id="job-noimage-1", proxy_dir=proxy, job_token="job-token-unit", objective=OBJECTIVE)
        )
    finally:
        workspace.rmdir()
        proxy.rmdir()
    assert asked == ["run"]
    assert isinstance(error, CodingSandboxError)
    assert error.code == "image_unavailable"


def test_a_local_job_fails_promptly_as_image_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _no_image(monkeypatch)
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    error = _run_bounded(lambda: run_coding_sandbox(tmp_path, job_id="job-noimage-2", objective=OBJECTIVE))
    assert isinstance(error, CodingSandboxError)
    assert error.code == "image_unavailable"


def test_image_unavailable_is_a_terminal_job_failure_not_a_stop_to_wait_on() -> None:
    assert "image_unavailable" not in coding_jobs._UNCONFIRMED
    assert "image_unavailable" not in coding_jobs._CONFIRMED_STOP


def test_the_runner_reports_image_unavailable_without_starting_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_image(monkeypatch)
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    body = {"op": "run", "workspace": str(workspace), "job_id": "job-noimage-3", "proxy_dir": str(proxy), "job_token": "job-token-unit", "objective": OBJECTIVE}
    try:
        status, reply = runner_process._sandbox(body)
    finally:
        workspace.rmdir()
        proxy.rmdir()
    assert status == 200
    assert reply["returncode"] != 0
    assert reply["stderr"] == "image_unavailable"
