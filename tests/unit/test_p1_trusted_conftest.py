"""A trusted conftest.py runs for the tests it applies to, as under plain pytest (Codex r4308195947).

Grading runs pytest with `--noconftest` (ADR-F44: the snapshot bridge does not load conftest files).
A baseline conftest's autouse fixture, teardown, fixtures or hooks therefore never ran: a test that
fails under normal pytest because of it passed here, and one that needs its fixture failed. Now the
grader hands each test process the BASELINE's conftest.py files on that module's directory chain (root
first, read from the baseline, never from the graded workspace), and the test process loads them from
those bytes as trusted plugins before the test module; `--noconftest` stays, so pytest itself still
reads no conftest from the workspace. One that is not in the baseline (the agent's) is never loaded.
A conftest that names `pytest_plugins` would make pytest import plugins by name, which the bridge does
not do: the suite fails as unsupported_conftest:pytest_plugins, by name, and no test runs.
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


_PLUGINS_REFUSED = {"suite": ("FAIL", "unsupported_conftest:pytest_plugins")}
_FIXTURE = "import pytest\n\nimport app\n\n@pytest.fixture\ndef answer():\n    return app.value()\n"
_USES_FIXTURE = "import app\n\ndef test_value(answer):\n    assert app.value() == answer\n"
_HOOK = "def pytest_runtest_setup(item):\n    raise AssertionError('setup hook refused')\n"
_PASS = {"test_app.test_value": ("PASS", "")}


def _verdicts(found: dict) -> dict:
    return {name: verdict for name, (verdict, _detail) in found.items()}


def test_the_conftest_fails_the_test_under_plain_pytest(tmp_path: Path) -> None:
    _write(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": _STRICT})
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AETHER_", "PYTEST_"))}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_app.py"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert "1 passed, 1 error" in completed.stdout, completed.stdout[-2000:]


def test_a_root_conftest_teardown_fails_the_test(tmp_path: Path) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": _STRICT})
    assert _verdicts(found) == {"test_app.test_value": "FAIL"}
    assert "teardown check failed" in found["test_app.test_value"][1] or "AssertionError" in found["test_app.test_value"][1]


def test_a_conftest_in_the_test_directory_applies(tmp_path: Path) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "tests/test_app.py": _TEST, "tests/conftest.py": _STRICT})
    assert _verdicts(found) == {"tests.test_app.test_value": "FAIL"}


@pytest.mark.parametrize("name", ["conftest.py", "pkg/conftest.py"])
def test_a_conftest_above_a_nested_test_module_applies(tmp_path: Path, name: str) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "pkg/sub/test_app.py": _TEST, name: _STRICT})
    assert _verdicts(found) == {"pkg.sub.test_app.test_value": "FAIL"}


def test_a_conftest_fixture_that_calls_candidate_code_is_provided(tmp_path: Path) -> None:
    assert _grade(tmp_path, {"app.py": _APP, "test_app.py": _USES_FIXTURE, "conftest.py": _FIXTURE}) == _PASS


def test_a_conftest_hook_runs(tmp_path: Path) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": _HOOK})
    assert _verdicts(found) == {"test_app.test_value": "FAIL"}


def test_a_conftest_no_test_module_loads_is_irrelevant(tmp_path: Path) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "other/conftest.py": _STRICT})
    assert found == _PASS


def test_a_conftest_that_is_not_in_the_baseline_is_never_loaded(tmp_path: Path) -> None:
    baseline = {"app.py": _APP, "test_app.py": _TEST}
    found = _grade(tmp_path, baseline, {**baseline, "conftest.py": _STRICT})
    assert found == _PASS


def test_the_baseline_conftest_is_loaded_even_if_the_workspace_copy_differs(tmp_path: Path) -> None:
    baseline = {"app.py": _APP, "test_app.py": _TEST, "conftest.py": _STRICT}
    found = _grade(tmp_path, baseline, {**baseline, "conftest.py": ""})
    assert _verdicts(found) == {"test_app.test_value": "FAIL"}


@pytest.mark.parametrize("text", ["", "# only a comment\n", '"""Docstring."""\npass\n', "import pytest\n", "def pytest_configure(config):\n    pass\n"])
def test_a_harmless_conftest_changes_nothing(tmp_path: Path, text: str) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": text})
    assert found == _PASS


def test_a_broken_conftest_fails_the_tests(tmp_path: Path) -> None:
    found = _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": "this is not python\n"})
    assert _verdicts(found) == {"test_app.test_value": "FAIL"}


@pytest.mark.parametrize("text", ["pytest_plugins = ['helper']\n", "pytest_plugins: list = []\n"])
def test_a_conftest_naming_plugins_is_refused_by_name(tmp_path: Path, text: str) -> None:
    assert _grade(tmp_path, {"app.py": _APP, "test_app.py": _TEST, "conftest.py": text}) == _PLUGINS_REFUSED
