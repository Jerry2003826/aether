"""Collection has its own limit: a test module slow to import (start-up plus collection) is not held to
one test's step_timeout, and when it does run out of time its tests are timed-out failures, never lost."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from aether_worker.coding_jobs import GRADING_VIOLATIONS

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _grade(root: Path, **extra: str) -> tuple[int, dict]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env["AETHER_PROTOCOL_SHM_DIR"] = "/dev/shm"
    env.update(extra)
    completed = subprocess.run([sys.executable, "-I", str(_PROTOCOL)], cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)
    line = next(item for item in reversed(completed.stdout.splitlines()) if item.startswith("{"))
    return completed.returncode, json.loads(line)


def _slow_module(root: Path, seconds: float) -> None:
    _write(
        root,
        {
            "app.py": "def value():\n    return 42\n",
            "test_app.py": (
                f"import time\nimport app\n\ntime.sleep({seconds})\n\n"
                "def test_first():\n    assert app.value() == 42\n\n"
                "def test_second():\n    assert app.value() == 42\n"
            ),
        },
    )


def test_collection_slower_than_one_test_step_is_not_cut_off_by_the_step_timeout(tmp_path: Path) -> None:
    # Collection takes longer than one test's 2 s, well within the collection limit: the tests run.
    _slow_module(tmp_path, 3)
    code, body = _grade(tmp_path, AETHER_PROTOCOL_TEST_TIMEOUT_SECONDS="2")
    cases = {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}
    assert "test_app.test_first" in cases and "test_app.test_second" in cases, body
    assert cases["test_app.test_first"] == ("PASS", ""), body
    assert cases["test_app.test_second"] == ("PASS", ""), body
    assert code == 0, body


def test_collection_past_its_limit_keeps_every_test_as_a_timed_out_failure(tmp_path: Path) -> None:
    # Collection outlasts both one test's step (2 s) and the collection limit (3 s).
    _slow_module(tmp_path, 8)
    code, body = _grade(tmp_path, AETHER_PROTOCOL_TEST_TIMEOUT_SECONDS="2", AETHER_PROTOCOL_COLLECT_TIMEOUT_SECONDS="3")
    cases = {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}
    assert "test_app.test_first" in cases and "test_app.test_second" in cases, body
    assert cases["test_app.test_first"] == ("FAIL", "timeout"), body
    assert cases["test_app.test_second"] == ("FAIL", "timeout"), body
    assert code == 1, body
    assert not any(case["detail"] in GRADING_VIOLATIONS or case["detail"].startswith("grading_") for case in body["cases"]), body
