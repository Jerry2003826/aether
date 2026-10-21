"""Worker startup says where it is (r11c, flake triage category A). Each startup step prints an
aether-worker-phase line, and a start that fails before aether-worker-ready prints one
aether-worker-start-failed line naming the step, so a wait on the worker can tell "slow but moving"
from "stuck" from "failed" instead of only timing out."""

from __future__ import annotations

import asyncio

import pytest
from aether_worker import service


def test_a_failed_role_launch_prints_start_failed_with_its_phase(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AETHER_WORKER_STATE_DIR", raising=False)

    def no_roles(*args, **kwargs):  # stands in for the role launch
        raise SystemExit("角色进程没有就绪")

    monkeypatch.setattr(service, "_endpoints_for_process", no_roles)
    with pytest.raises(SystemExit):
        asyncio.run(service.serve())
    lines = capsys.readouterr().out.splitlines()
    failed = [line for line in lines if line.startswith("aether-worker-start-failed ")]
    assert failed == ["aether-worker-start-failed phase=roles error=SystemExit"], lines
    assert "aether-worker-ready" not in lines
    phases = [line.split()[1] for line in lines if line.startswith("aether-worker-phase ")]
    assert phases[0] == "name=init", lines


def test_a_failed_temporal_connect_names_the_connect_phase(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AETHER_WORKER_STATE_DIR", raising=False)
    endpoints = service.WorkerEndpoints(
        worker_url="postgresql://unused/x",
        dispatcher_url="postgresql://unused/x",
        verifier_url="",
        artifact_root=tmp_path / "artifacts",
        temporal_address="127.0.0.1:1",
        task_queue="r11c-phase",
    )

    async def refused(*args, **kwargs):
        raise ConnectionRefusedError("temporal down")

    monkeypatch.setattr(service.Client, "connect", refused)
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(service.serve(endpoints))
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line.startswith("aether-worker-start-failed ")] == [
        "aether-worker-start-failed phase=temporal-connect error=ConnectionRefusedError"
    ], lines
