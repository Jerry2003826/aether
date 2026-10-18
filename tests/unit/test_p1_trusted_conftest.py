"""A trusted conftest.py the bound suite relies on is refused, never silently dropped (Codex r4308195947).

Grading runs pytest with `--noconftest` (ADR-F44: the snapshot bridge does not load conftest files).
A baseline conftest's autouse fixture, teardown or hook therefore never ran: a test that fails under
normal pytest because of it passed here, and a patch that did not pass the complete bound suite could
be published. Running the conftest in the trusted process is not possible through the snapshot bridge,
so a suite with a baseline conftest.py that pytest would load for one of its test modules (in the
module's directory or a parent up to the root) fails as a whole with `unsupported_conftest` and no
test runs. A conftest pytest would not load for any test module is irrelevant; a conftest that is not
in the baseline (the agent's) is removed by the restore and never configures grading.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "trusted conftest nonce"

_APP = "def value():\n    return 1\n"
_TEST = "import app\n\ndef test_value():\n    assert app.value() == 1\n"
# A trusted conftest whose autouse teardown fails every test (in-process pytest reports an error).
_STRICT = "import pytest\n\n@pytest.fixture(autouse=True)\ndef strict():\n    yield\n    raise AssertionError('teardown check failed')\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _grade(tmp_path: Path, baseline: dict[str, str], job: dict[str, str] | None = None) -> dict:
    base, work = tmp_path / "baseline", tmp_path / "job"
    _write(base, baseline)
    _write(work, baseline if job is None else job)
    secret = tmp_path / "nonce"
    secret.write_text(_NONCE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AETHER_", "PYTEST_"))}
    env.update({"AETHER_PROTOCOL_NONCE_FILE": str(secret), "AETHER_PROTOCOL_SHM_DIR": "/dev/shm", "AETHER_PROTOCOL_BASELINE_DIR": str(base)})
    completed = subprocess.run([sys.executable, "-I", str(_PROTOCOL)], cwd=work, env=env, capture_output=True, text=True, timeout=120, check=False)
    signed = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{") and _NONCE in line]
    assert len(signed) == 1, completed.stdout[-3000:] + completed.stderr[-3000:]
    return {case["name"]: (case["verdict"], case["detail"]) for case in signed[0]["cases"]}


_REFUSED = {"suite": ("FAIL", "unsupported_conftest")}


def test_the_conftest_fails_the_test_under_plain_pytest(tmp_path: Path) -> None:
    _write(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": _STRICT})
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AETHER_", "PYTEST_"))}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_app.py"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert "1 passed, 1 error" in completed.stdout, completed.stdout[-2000:]


def test_a_root_conftest_refuses_the_suite(tmp_path: Path) -> None:
    assert _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": _STRICT}) == _REFUSED


def test_a_conftest_in_the_test_directory_refuses_the_suite(tmp_path: Path) -> None:
    assert _grade(tmp_path, {"app.py": _APP, "tests/test_app.py": _TEST, "tests/conftest.py": _STRICT}) == _REFUSED


def test_a_conftest_no_test_module_loads_is_irrelevant(tmp_path: Path) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "other/conftest.py": _STRICT})
    assert found == {"test_app.test_value": ("PASS", "")}


def test_a_conftest_that_is_not_in_the_baseline_does_not_refuse(tmp_path: Path) -> None:
    baseline = {"app.py": _APP, "test_app.py": _TEST}
    found = _grade(tmp_path, baseline, {**baseline, "conftest.py": _STRICT})
    assert found == {"test_app.test_value": ("PASS", "")}


@pytest.mark.parametrize("name", ["conftest.py", "pkg/conftest.py"])
def test_a_conftest_above_a_nested_test_module_refuses_the_suite(tmp_path: Path, name: str) -> None:
    files = {"app.py": _APP, "pkg/sub/test_app.py": _TEST, name: _STRICT}
    assert _grade(tmp_path, files) == _REFUSED
