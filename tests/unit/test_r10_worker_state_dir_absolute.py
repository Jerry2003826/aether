"""The worker state directory (where the coding instance id is kept) is resolved once, to an absolute
path, when the worker starts: the default data/worker or a relative AETHER_WORKER_STATE_DIR is taken
against the worker's working directory, written back to AETHER_WORKER_STATE_DIR and printed. The
runner gets that absolute path, so a runner started in another directory reads the same instance id
and labels and sweeps the same containers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from aether_worker import coding_sandbox, role_launch, service


class _Stop(Exception):
    pass


def _start_worker(monkeypatch: pytest.MonkeyPatch, cwd: Path) -> None:
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(service, "warn_if_git_missing", lambda: None)
    monkeypatch.setattr(service, "warn_coding_rate_default", lambda: None)
    monkeypatch.setattr(service, "stop_children", lambda children: None)
    monkeypatch.setattr(service, "stop_recorded_roles", lambda: None)

    def handoff() -> None:
        raise _Stop

    monkeypatch.setattr(service, "_endpoints_for_process", handoff)
    with pytest.raises(_Stop):
        asyncio.run(service.serve())


def _as_runner(monkeypatch: pytest.MonkeyPatch, cwd: Path) -> str:
    env = role_launch._runner_env("loopback-unit")
    for name in ("AETHER_WORKER_STATE_DIR", "AETHER_WORKER_INSTANCE_ID"):
        if name in env:
            monkeypatch.setenv(name, env[name])
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(cwd)
    return coding_sandbox.worker_instance_id()


@pytest.mark.parametrize("configured", [None, "state/relative-worker"])
def test_a_runner_in_another_directory_reads_the_workers_instance_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], configured: str | None
) -> None:
    worker_cwd, runner_cwd = tmp_path / "worker-cwd", tmp_path / "runner-cwd"
    worker_cwd.mkdir()
    runner_cwd.mkdir()
    monkeypatch.delenv("AETHER_WORKER_INSTANCE_ID", raising=False)
    if configured is None:
        monkeypatch.delenv("AETHER_WORKER_STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("AETHER_WORKER_STATE_DIR", configured)
    _start_worker(monkeypatch, worker_cwd)
    expected = (worker_cwd / (configured or "data/worker")).resolve()
    assert os.environ["AETHER_WORKER_STATE_DIR"] == str(expected)
    assert f"aether-worker-state-dir path={expected}" in capsys.readouterr().out
    worker_id = coding_sandbox.worker_instance_id()
    assert (expected / "coding-instance-id").read_text(encoding="ascii").strip() == worker_id
    assert _as_runner(monkeypatch, runner_cwd) == worker_id
    assert not (runner_cwd / "data").exists()


def test_an_absolute_state_dir_is_kept_as_it_is(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "fixed-state"
    (tmp_path / "cwd").mkdir()
    monkeypatch.setenv("AETHER_WORKER_STATE_DIR", str(state))
    _start_worker(monkeypatch, tmp_path / "cwd")
    assert os.environ["AETHER_WORKER_STATE_DIR"] == str(state)
    assert f"aether-worker-state-dir path={state}" in capsys.readouterr().out
