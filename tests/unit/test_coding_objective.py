"""The job objective reaches the sandbox as one docker -e argument, through every launch path."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
from aether_worker import coding_jobs, coding_sandbox, runner_process, sandbox_delegate
from aether_worker.coding_jobs import CodingError
from aether_worker.coding_sandbox import (
    CodingSandboxError,
    container_arguments,
    run_coding_sandbox,
)
from aether_worker.runner import JOB_IMAGE

# Quotes, a newline, command substitution, backticks, braces, a format marker and a fake flag.
OBJECTIVE = (
    "Rename \"value\" to 'answer' in app.py\n"
    "then $(touch /tmp/pwned) && `id`; echo ${HOME} {target} {0} %s \\n -e AETHER_JOB_TOKEN=x 中文"
)


def _env_values(args: list[str]) -> list[str]:
    return [args[index + 1] for index, part in enumerate(args[:-1]) if part == "-e"]


def test_container_arguments_carry_the_objective_as_one_env_argument() -> None:
    args = container_arguments(Path("."), JOB_IMAGE, objective=OBJECTIVE)
    assert f"AETHER_OBJECTIVE={OBJECTIVE}" in _env_values(args)
    position = args.index(f"AETHER_OBJECTIVE={OBJECTIVE}")
    assert args[position - 1] == "-e"
    assert position < args.index(JOB_IMAGE)
    # The entry command is unchanged and no shell wraps it.
    assert args[-3:] == ["/opt/openhands/.venv/bin/python", "-I", "/opt/aether/openhands_sandbox_entry.py"]
    assert "sh" not in args and "-c" not in args
    # The fake flag inside the objective is not a separate argument.
    assert "AETHER_JOB_TOKEN=x" not in args


@pytest.mark.parametrize("bad", ["", "   \n", "a\x00b", "x" * 4001], ids=["empty", "blank", "nul", "too_long"])
def test_container_arguments_reject_a_missing_or_invalid_objective(bad: str) -> None:
    with pytest.raises(CodingSandboxError) as rejected:
        container_arguments(Path("."), JOB_IMAGE, objective=bad)
    assert rejected.value.code == "objective_invalid"


def test_local_launch_passes_the_objective_to_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: [])
    monkeypatch.setattr(coding_sandbox, "launch_image", lambda image, _default: image)
    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", lambda *_a, **_k: {"result": {}})

    def docker(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(["docker"], 0, "cid\n", "")

    monkeypatch.setattr(coding_sandbox, "_docker", docker)
    run_coding_sandbox(tmp_path, job_id="job-local-1", objective=OBJECTIVE)
    assert len(calls) == 1
    assert calls[0][:2] == ["run", "-d"]
    assert f"AETHER_OBJECTIVE={OBJECTIVE}" in _env_values(calls[0])


def test_delegated_launch_sends_the_objective_and_the_runner_passes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict]] = []
    launched: list[list[str]] = []
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda _job: [])
    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", lambda *_a, **_k: {"result": {}})
    monkeypatch.setattr(runner_process, "launch_image", lambda image, _default: image)

    def runner_docker(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        launched.append(args)
        return subprocess.CompletedProcess(["docker"], 0, "cid\n", "")

    monkeypatch.setattr(runner_process, "_docker", runner_docker)

    def ask(op: str, fields: dict, *, timeout: float | None) -> dict:
        sent.append((op, dict(fields)))
        status, body = runner_process._sandbox({"op": op, **fields})
        assert status == 200, body
        return body

    monkeypatch.setattr(sandbox_delegate, "_ask", ask)
    try:
        run_coding_sandbox(workspace, job_id="job-remote-1", proxy_dir=proxy, job_token="job-token-unit", objective=OBJECTIVE)
    finally:
        workspace.rmdir()
        proxy.rmdir()
    assert [op for op, _fields in sent] == ["run"]
    assert sent[0][1]["objective"] == OBJECTIVE
    assert len(launched) == 1
    assert f"AETHER_OBJECTIVE={OBJECTIVE}" in _env_values(launched[0])


def test_runner_rejects_a_run_without_an_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    monkeypatch.setattr(runner_process, "launch_image", lambda image, _default: image)
    monkeypatch.setattr(runner_process, "_docker", lambda *_a, **_k: pytest.fail("docker called without an objective"))
    body = {"op": "run", "workspace": str(workspace), "job_id": "job-remote-2", "proxy_dir": str(proxy), "job_token": "job-token-unit"}
    try:
        status, rejected = runner_process._sandbox(body)
        blank_status, blank = runner_process._sandbox({**body, "objective": " "})
    finally:
        workspace.rmdir()
        proxy.rmdir()
    assert (status, rejected) == (400, {"error": "rejected"})
    assert (blank_status, blank) == (400, {"error": "rejected"})


def test_job_objective_comes_from_the_claimed_row_verbatim() -> None:
    assert coding_jobs._job_objective({"objective": OBJECTIVE}) == OBJECTIVE
    for row in ({}, {"objective": None}, {"objective": ""}, {"objective": "  "}):
        with pytest.raises(CodingError) as missing:
            coding_jobs._job_objective(row)
        assert missing.value.code == "objective_missing"
