"""The real worker process start: phase lines, start-failed, and a slow start that keeps progressing."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from aether_api.auth_service import issue_bootstrap_secret
from aether_worker import startup_budget
from tests.integration.test_worker_process import ROOT, _port, _service_env, _start, _stop, _temporal_address

# role_hook is passed to serve() by this script only; nothing reads it from the environment.
_SLOW_START = """
import asyncio, sys, time
from aether_worker.service import serve
calls = []
def role_hook(role):
    calls.append(role)
    print(f"aether-test-role-hook role={role} order={','.join(calls)}", flush=True)
    for i in range(10):
        time.sleep(5)
        print(f"aether-test-role-progress role={role} step={i}", flush=True)
asyncio.run(serve(role_hook=role_hook))
"""


def _env(role_urls: dict[str, str], tmp_path: Path, address: str) -> dict[str, str]:
    secret_path = ROOT / "secrets" / f"r11c-startup-{uuid.uuid4().hex}.json"
    issue_bootstrap_secret(secret_path)
    env = _service_env(
        role_urls,
        address=address,
        api_port=_port(),
        queue=f"aether-r11c-{uuid.uuid4()}",
        artifact_root=tmp_path / "artifacts",
        secret_path=secret_path,
        index_root=tmp_path / "index",
    )
    env.pop("AETHER_LIVE_MODEL_KEY", None)
    return env


def test_a_slow_but_progressing_worker_start_is_waited_for(role_urls, clean_db, tmp_path: Path) -> None:
    env = _env(role_urls, tmp_path, _temporal_address())
    logs: list[str] = []
    worker = _start([sys.executable, "-c", _SLOW_START], env, logs)
    try:
        clock = startup_budget.wait_worker_ready(logs, worker)
        text = "".join(logs)
        # 2 roles (no provider key, no gateway) x 50s of hook progress: longer than any single stall.
        assert clock.elapsed > 2 * 50
        assert "aether-test-role-hook role=verifier order=verifier\n" in text
        assert "aether-test-role-hook role=runner order=verifier,runner\n" in text
        for step in ("image-ready", "roles-ready", "temporal-connect", "worker-start"):
            assert f"{startup_budget.PHASE} step={step}" in text, text[-2000:]
        assert startup_budget.START_FAILED not in text
    finally:
        _stop(worker)


def test_an_unreachable_temporal_reports_the_failed_phase(role_urls, clean_db, tmp_path: Path) -> None:
    env = _env(role_urls, tmp_path, f"127.0.0.1:{_port()}")
    logs: list[str] = []
    worker = _start([sys.executable, "-m", "aether_worker"], env, logs)
    try:
        with pytest.raises(startup_budget.WorkerStartFailed, match="phase=temporal-connect"):
            startup_budget.wait_worker_ready(logs, worker)
    finally:
        _stop(worker)
