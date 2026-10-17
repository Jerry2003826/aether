"""Workspace modules never take a stdlib or installed-dependency name, in the test or the candidate process,
and tests are only served candidate modules the baseline declared (ADR-F44)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _grade(root: Path, baseline: Path | None = None) -> tuple[int, dict]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    if baseline is not None:
        env["AETHER_PROTOCOL_BASELINE_DIR"] = str(baseline)
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)], cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    line = next(item for item in reversed(completed.stdout.splitlines()) if item.startswith("{"))
    return completed.returncode, json.loads(line)


def _verdicts(body: dict) -> dict[str, tuple[str, str]]:
    return {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}


def test_a_test_dir_module_named_like_stdlib_shadows_it_for_neither_process(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import fractions\nimport types\n\n"
                "def half():\n    return str(fractions.Fraction(1, 2))\n\n"
                "def namespace():\n    return types.SimpleNamespace(a=1).a\n"
            ),
            "tests/types.py": "SimpleNamespace = None\n",
            "tests/fractions.py": "raise RuntimeError('tests/fractions.py shadowed stdlib fractions')\n",
            "tests/test_std.py": (
                "import fractions\nimport types\nimport app\n\n"
                "def test_test_process_gets_stdlib():\n"
                "    assert fractions.Fraction(1, 2) == fractions.Fraction(2, 4)\n"
                "    assert types.SimpleNamespace is not None\n\n"
                "def test_candidate_process_gets_stdlib():\n"
                "    assert app.half() == '1/2'\n    assert app.namespace() == 1\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    assert _verdicts(body) == {
        "tests.test_std.test_test_process_gets_stdlib": ("PASS", ""),
        "tests.test_std.test_candidate_process_gets_stdlib": ("PASS", ""),
    }, body
    assert code == 0, body


def test_a_module_named_like_an_installed_dependency_is_never_served_in_its_place(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "requests.py": "Session = None\n",
            "app.py": "import requests\n\ndef has_session():\n    return callable(requests.Session)\n",
            "test_dep.py": (
                "import requests\nimport app\n\n"
                "def test_test_process_gets_the_dependency():\n    assert callable(requests.Session)\n\n"
                "def test_candidate_process_gets_the_dependency():\n    assert app.has_session() is True\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    assert _verdicts(body) == {
        "test_dep.test_test_process_gets_the_dependency": ("PASS", ""),
        "test_dep.test_candidate_process_gets_the_dependency": ("PASS", ""),
    }, body
    assert code == 0, body


def test_tests_are_served_only_candidate_modules_the_baseline_declared(tmp_path: Path) -> None:
    baseline = {
        "app.py": "def value():\n    return 0\n",
        "tests/helpers.py": "VALUE = 7\n",
        "tests/test_x.py": (
            "import app\nimport helpers\n\n"
            "try:\n    import extra\nexcept ImportError:\n    extra = None\n\n"
            "try:\n    import helper_new\nexcept ImportError:\n    helper_new = None\n\n"
            "def test_new_root_module_is_not_served_to_tests():\n    assert extra is None\n\n"
            "def test_new_helper_in_the_test_dir_is_not_served_to_tests():\n    assert helper_new is None\n\n"
            "def test_baseline_helper_is_served():\n    assert helpers.VALUE == 7\n\n"
            "def test_candidate_code_can_import_its_new_module():\n    assert app.value() == 42\n"
        ),
    }
    work = tmp_path / "work"
    job = tmp_path / "job"
    _write(work, baseline)
    shutil.copytree(work, job)
    _write(
        job,
        {
            "app.py": "import extra\n\ndef value():\n    return extra.VALUE\n",
            "extra.py": "VALUE = 42\n",
            "tests/helper_new.py": "VALUE = 1\n",
        },
    )
    code, body = _grade(job, baseline=work)
    assert _verdicts(body) == {
        "tests.test_x.test_new_root_module_is_not_served_to_tests": ("PASS", ""),
        "tests.test_x.test_new_helper_in_the_test_dir_is_not_served_to_tests": ("PASS", ""),
        "tests.test_x.test_baseline_helper_is_served": ("PASS", ""),
        "tests.test_x.test_candidate_code_can_import_its_new_module": ("PASS", ""),
    }, body
    assert code == 0, body


def test_the_sandbox_entry_tells_the_protocol_where_the_baseline_is() -> None:
    source = Path("services/worker/aether_worker/openhands_sandbox_entry.py").read_text(encoding="utf-8")
    assert "AETHER_PROTOCOL_BASELINE_DIR={shlex.quote(str(WORK))}" in source


def test_a_test_package_named_like_stdlib_still_runs_its_tests(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            "test/__init__.py": "",
            "test/test_pkg.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert _verdicts(body) == {"test.test_pkg.test_value": ("PASS", "")}, body
    assert code == 0, body
