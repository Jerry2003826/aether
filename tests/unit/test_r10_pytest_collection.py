"""The protocol collects and runs a trusted suite as pytest does (Codex 13:16 on a3c7654).

Each case is a suite a plain `pytest` run fails; the protocol must fail it too, with the failing test
named, instead of dropping the test (a name or file it did not recognise) or changing its fixture scope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "n0nce-5b1e9d03c7a24f86"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _grade(root: Path) -> tuple[int, dict[str, tuple[str, str]], str]:
    """(exit code, {case name: (verdict, detail)} from the nonce-signed line, raw output)."""
    secret = root.parent / (root.name + ".nonce")
    secret.write_text(_NONCE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env["AETHER_PROTOCOL_NONCE_FILE"] = str(secret)
    env["AETHER_PROTOCOL_SHM_DIR"] = "/dev/shm"
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)], cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    output = completed.stdout + completed.stderr
    signed = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{") and _NONCE in line]
    assert len(signed) == 1, output
    body = signed[0]
    assert body["nonce"] == _NONCE, output
    return completed.returncode, {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}, output


_APP = "def value():\n    return 1\n"


def test_a_failing_top_level_test_named_with_the_bare_test_prefix_fails_the_suite(tmp_path: Path) -> None:
    # pytest's python_functions is `test`: testFailure is a test, exactly like test_ok.
    _write(
        tmp_path,
        {
            "app.py": _APP,
            "test_mod.py": (
                "import app\n\n"
                "def test_ok():\n    assert app.value() == 1\n\n"
                "def testFailure():\n    assert app.value() == 2\n\n"
                "def testPasses():\n    assert app.value() == 1\n"
            ),
        },
    )
    code, verdicts, output = _grade(tmp_path)
    assert verdicts == {
        "test_mod.test_ok": ("PASS", ""),
        "test_mod.testFailure": ("FAIL", "AssertionError"),
        "test_mod.testPasses": ("PASS", ""),
    }, output
    assert code == 1, output
