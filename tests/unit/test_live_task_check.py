"""The live check and F26 scripts stay blocked without a provider, and never print a key."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY = "local-check-token"


def _without_provider() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "AETHER_DATABASE_URL",
        "AETHER_WORKER_DATABASE_URL",
        "AETHER_VERIFIER_DATABASE_URL",
        "AETHER_DISPATCHER_DATABASE_URL",
        "AETHER_TEMPORAL_ADDRESS",
        "AETHER_LIVE_MODEL_BASE",
        "AETHER_LIVE_MODEL_ALIAS",
        "AETHER_LIVE_MODEL_KEY",
    ):
        env.pop(name, None)
    env["AETHER_LIVE_MODEL_KEY"] = KEY
    return env


def test_live_task_check_blocks_without_printing_the_key() -> None:
    target = ROOT / "reports" / "live_task_check.json"
    previous = target.read_bytes() if target.exists() else None
    try:
        completed = subprocess.run(
            [sys.executable, "scripts/run_live_task_check.py", "--out", "reports/live_task_check.json"],
            cwd=ROOT,
            env=_without_provider(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2
        assert KEY not in completed.stdout
        assert KEY not in completed.stderr
        written = target.read_text(encoding="utf-8")
        assert KEY not in written
        assert '"status": "blocked"' in written
    finally:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous)


def test_f26_acceptance_script_blocks_without_printing_the_key() -> None:
    target = ROOT / "reports" / "f26_acceptance_result.json"
    previous = target.read_bytes()
    try:
        completed = subprocess.run(
            [sys.executable, "scripts/run_f26_acceptance.py", "--out", "reports/f26_acceptance_result.json"],
            cwd=ROOT,
            env=_without_provider(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2
        assert KEY not in completed.stdout
        assert KEY not in completed.stderr
        assert KEY not in target.read_text(encoding="utf-8")
    finally:
        target.write_bytes(previous)
