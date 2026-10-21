"""A worker whose roles come up slowly, but keep coming up, is waited for (r11c, triage category A).

The roles are delayed by a hook passed straight into serve() from a test entry point (dependency
injection, off by default; production startup reads no switch for it). Each role is held 45 s before
its readiness wait: every step stays inside the stall limit, the whole start inside the product's
budget, but the start as a whole takes longer than the fixed 90 s the old wait allowed. This is the
"aether-worker-ready not seen" flake made deterministic."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from aether_worker import startup_budget

from tests.integration.startup_wait import wait_worker_ready
from tests.integration.test_worker_process import (
    ROOT,
    _port,
    _service_env,
    _start,
    _stop,
    _temporal_address,
)

_ROLE_DELAY = 45.0
_ENTRY = f"""
import asyncio, time
from aether_worker.service import serve
asyncio.run(serve(role_hook=lambda role: time.sleep({_ROLE_DELAY})))
"""


def test_a_slow_but_progressing_worker_start_is_waited_for(role_urls, clean_db, tmp_path: Path) -> None:
    assert _ROLE_DELAY < startup_budget.STALL_LIMIT
    env = _service_env(
        role_urls,
        address=_temporal_address(),
        api_port=_port(),
        queue=f"r11c-slow-{uuid.uuid4()}",
        artifact_root=tmp_path / "artifacts",
        secret_path=ROOT / "secrets" / f"r11c-slow-{uuid.uuid4().hex}.json",
        index_root=tmp_path / "indexes",
    )
    logs: list[str] = []
    worker = _start([sys.executable, "-c", _ENTRY], env, logs)
    try:
        wait_worker_ready(logs, worker)
        roles = [line for line in logs if line.startswith("aether-worker-phase name=role-ready ")]
        assert len(roles) >= 2, "".join(logs)
    finally:
        _stop(worker)
