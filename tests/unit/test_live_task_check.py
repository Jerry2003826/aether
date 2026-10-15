"""The live check and F26 scripts stay blocked without a provider, and never print a key."""

from __future__ import annotations

import importlib.util
import json
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


def test_exception_report_records_step_type_and_redacted_message() -> None:
    path = ROOT / "scripts" / "run_live_task_check.py"
    spec = importlib.util.spec_from_file_location("run_live_task_check_exc", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    previous = os.environ.get(module.LIVE_KEY)
    os.environ[module.LIVE_KEY] = KEY
    try:
        try:
            raise KeyError("artifact_id")
        except KeyError as exc:
            missing = module._exception_report("read_upload", exc)
        try:
            raise RuntimeError(f"upstream rejected {KEY}")
        except RuntimeError as exc:
            leaked = module._exception_report("create_task", exc)
    finally:
        if previous is None:
            os.environ.pop(module.LIVE_KEY, None)
        else:
            os.environ[module.LIVE_KEY] = previous
    assert missing["status"] == "failed"
    assert missing["reason"] == "exception"
    assert missing["step"] == "read_upload"
    assert missing["error_type"] == "KeyError"
    assert "artifact_id" in str(missing["error_message"])
    encoded = json.dumps({"missing": missing, "leaked": leaked}, ensure_ascii=False)
    assert KEY not in encoded
    assert leaked["step"] == "create_task"
    assert leaked["error_type"] == "RuntimeError"
    assert "[REDACTED]" in str(leaked["error_message"])


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
