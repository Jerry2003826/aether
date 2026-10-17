"""A coding result whose patch is far above 64 KiB reaches the worker intact."""

from __future__ import annotations

import json
import subprocess

import pytest

from aether_worker import coding_sandbox, runner_process

_NAME = "aether-code-job1"


def _stdout(patch: str) -> bytes:
    noise = "agent log line\n" * 5000
    body = {"passed": 1, "failed": 0, "errors": 0, "cases": [], "patch": patch}
    return (noise + "RESULT " + json.dumps(body, separators=(",", ":")) + "\n").encode()


def _large_patch() -> str:
    lines = [f"+line {index:06d} " + "x" * 40 + "\n" for index in range(4000)]
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,4000 @@\n" + "".join(lines)
    assert len(patch.encode()) > 200 * 1024
    return patch


def _fake_docker(args, *, timeout=None):
    if args[:2] == ["inspect", "-f"]:
        return subprocess.CompletedProcess(args, 0, "0\n", "")
    return subprocess.CompletedProcess(args, 0, "", "")


@pytest.mark.parametrize("delegated", [False, True])
def test_large_patch_round_trips_intact(monkeypatch, delegated: bool) -> None:
    patch = _large_patch()
    stdout = _stdout(patch)

    def logs(name, *, limit):
        assert name == _NAME
        return stdout[-limit:], b""

    monkeypatch.setattr(coding_sandbox, "container_logs", logs)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", _fake_docker)
    # The worker delegates; the runner process that serves the logs request does not.
    state = {"delegates": delegated}
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: state["delegates"])

    def remote(name, limit, *, timeout=None):
        state["delegates"] = False
        try:
            status, body = runner_process._sandbox({"op": "logs", "name": name, "limit": limit})
        finally:
            state["delegates"] = True
        assert status == 200
        return body

    monkeypatch.setattr(coding_sandbox, "remote_logs", remote)
    collected = coding_sandbox._collect_exit(_NAME)
    assert collected["result"]["patch"] == patch
    assert len(collected["stdout_tail"]) <= coding_sandbox.LOG_TAIL_LIMIT
