"""Candidate code that runs during grading cannot change what the remaining grading files say."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()


def _grade(root: Path) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    line = next(item for item in reversed(completed.stdout.splitlines()) if item.startswith("{"))
    return completed.returncode, json.loads(line)


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_candidate_rewriting_a_later_test_file_cannot_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            # Imported by the first test module; rewrites the second before it is loaded.
            "app.py": (
                "import pathlib\n"
                "pathlib.Path(__file__).with_name('test_b.py').write_text('def test_b():\\n    pass\\n')\n"
                "def value():\n    return 0\n"
            ),
            "test_a.py": "import app\n\ndef test_a():\n    assert True\n",
            "test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert code == 1, body
    assert body["passed"] < 2
    assert body["failed"] >= 1
    assert not any(case["name"] == "test_b.test_b" and case["verdict"] == "PASS" for case in body["cases"]), body


def test_candidate_rewriting_a_support_module_cannot_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import pathlib\n"
                "pathlib.Path(__file__).with_name('oracle.py').write_text('def expected():\\n    return 0\\n')\n"
                "def value():\n    return 0\n"
            ),
            "oracle.py": "def expected():\n    return 42\n",
            "test_a.py": "import app\n\ndef test_a():\n    assert True\n",
            "test_b.py": "import app\nimport oracle\n\ndef test_b():\n    assert app.value() == oracle.expected()\n",
        },
    )
    code, body = _grade(tmp_path)
    assert code == 1, body
    assert body["failed"] >= 1
    assert not any(case["name"] == "test_b.test_b" and case["verdict"] == "PASS" for case in body["cases"]), body


def test_tampering_during_the_run_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import pathlib\n"
                "pathlib.Path(__file__).with_name('test_b.py').write_text('def test_b():\\n    pass\\n')\n"
                "def value():\n    return 42\n"
            ),
            "test_a.py": "import app\n\ndef test_a():\n    assert app.value() == 42\n",
            "test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert code == 1, body
    assert {"name": "suite", "verdict": "FAIL", "detail": "grading_tampered"} in body["cases"]


def test_an_honest_suite_still_passes_and_packages_import(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg.core import answer\n",
            "pkg/core.py": "def answer():\n    return 42\n",
            "app.py": "from pkg import answer\n\ndef value():\n    return answer()\n",
            "oracle.py": "def expected():\n    return 42\n",
            "test_a.py": "import app\nimport oracle\n\ndef test_a():\n    assert app.value() == oracle.expected()\n",
            "test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n\nasync def test_c():\n    pass\n",
        },
    )
    code, body = _grade(tmp_path)
    assert body["passed"] == 2, body
    assert {"name": "test_b.test_c", "verdict": "FAIL", "detail": "async"} in body["cases"]
    assert code == 1
    (tmp_path / "test_b.py").write_text("import app\n\ndef test_b():\n    assert app.value() == 42\n", encoding="utf-8")
    code, body = _grade(tmp_path)
    assert (code, body["passed"], body["failed"]) == (0, 2, 0), body


def test_new_files_and_non_grading_data_written_during_the_run_are_fine(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            "data.json": "{}\n",
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
            ".venv/lib/site.py": "x = 1\n",
            "node_modules/pkg/index.js": "module.exports = 1\n",
            "test_a.py": (
                "import pathlib\nimport app\n\n"
                "def test_a():\n"
                "    pathlib.Path('run.log').write_text('log line\\n')\n"
                "    pathlib.Path('tmp/out.txt').parent.mkdir()\n"
                "    pathlib.Path('tmp/out.txt').write_text('x')\n"
                "    pathlib.Path('data.json').write_text('{\"seen\": 1}\\n')\n"
                "    pathlib.Path('.venv/lib/site.py').write_text('x = 2\\n')\n"
                "    pathlib.Path('node_modules/pkg/index.js').write_text('changed')\n"
                "    assert app.value() == 42\n"
            ),
            "test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert not any(case["detail"] == "grading_tampered" for case in body["cases"]), body
    assert (code, body["passed"], body["failed"]) == (0, 2, 0), body


def test_deleting_or_changing_a_grading_file_is_still_tampering(tmp_path: Path) -> None:
    for index, script in enumerate(
        (
            "pathlib.Path('oracle.py').unlink()",
            "pathlib.Path('pyproject.toml').write_text('[tool.x]\\n')",
        )
    ):
        root = tmp_path / str(index)
        root.mkdir()
        _write(
            root,
            {
                "app.py": "def value():\n    return 42\n",
                "oracle.py": "def expected():\n    return 42\n",
                "pyproject.toml": "[project]\nname = 'x'\n",
                "test_a.py": f"import pathlib\nimport app\n\ndef test_a():\n    {script}\n    assert app.value() == 42\n",
            },
        )
        code, body = _grade(root)
        assert code == 1, body
        assert {"name": "suite", "verdict": "FAIL", "detail": "grading_tampered"} in body["cases"], body


def test_a_candidate_package_named_env_or_venv_is_tamper_checked(tmp_path: Path) -> None:
    for index, package in enumerate(("env", "venv", ".venv")):
        root = tmp_path / str(index)
        root.mkdir()
        _write(
            root,
            {
                f"{package}/__init__.py": "",
                f"{package}/core.py": "def value():\n    return 42\n",
                "test_a.py": (
                    "import pathlib\n\ndef test_a():\n"
                    f"    pathlib.Path('{package}/core.py').write_text('def value():\\n    return 0\\n')\n"
                ),
                "test_b.py": "def test_b():\n    pass\n",
            },
        )
        code, body = _grade(root)
        assert code == 1, (package, body)
        assert {"name": "suite", "verdict": "FAIL", "detail": "grading_tampered"} in body["cases"], (package, body)


def test_a_real_virtualenv_with_pyvenv_cfg_is_skipped(tmp_path: Path) -> None:
    for index, venv in enumerate(("env", "venv", ".venv")):
        root = tmp_path / str(index)
        root.mkdir()
        _write(
            root,
            {
                f"{venv}/pyvenv.cfg": "home = /usr/bin\n",
                f"{venv}/lib/python3/site-packages/dep.py": "x = 1\n",
                "test_a.py": (
                    "import pathlib\n\ndef test_a():\n"
                    f"    pathlib.Path('{venv}/lib/python3/site-packages/dep.py').write_text('x = 2\\n')\n"
                    f"    pathlib.Path('{venv}/pyvenv.cfg').write_text('home = /tmp\\n')\n"
                ),
            },
        )
        code, body = _grade(root)
        assert (code, body["passed"], body["failed"]) == (0, 1, 0), (venv, body)


def test_a_failing_test_in_a_subdirectory_fails_the_suite(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 0\n",
            "test_a.py": "def test_a():\n    pass\n",
            "tests/test_strict.py": "import app\n\ndef test_strict():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert code == 1, body
    assert {"name": "tests.test_strict.test_strict", "verdict": "FAIL", "detail": "AssertionError"} in body["cases"], body


def test_subdirectory_tests_are_discovered_run_and_counted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            "oracle.py": "def expected():\n    return 42\n",
            "test_a.py": "def test_a():\n    pass\n",
            # Not a package: sibling helpers import by bare name, like pytest's default rootdir mode.
            "tests/helpers.py": "import oracle\n\ndef want():\n    return oracle.expected()\n",
            "tests/test_plain.py": "import app\nimport helpers\n\ndef test_plain():\n    assert app.value() == helpers.want()\n",
            # A package with a nested non-package directory.
            "pkgtests/__init__.py": "",
            "pkgtests/support.py": "VALUE = 42\n",
            "pkgtests/test_pkg.py": "import app\nfrom pkgtests.support import VALUE\n\ndef test_pkg():\n    assert app.value() == VALUE\n",
            "tests/unit/test_x.py": "import app\n\ndef test_x():\n    assert app.value() == 42\n",
            "test/deep/check_test.py": "import app\n\ndef test_suffix():\n    assert app.value() == 42\n",
            # Same basename at two levels: both run, neither hides the other.
            "tests/unit/test_a.py": "def test_a():\n    pass\n",
            # Skipped trees are never collected.
            "node_modules/x/test_ignored.py": "def test_ignored():\n    raise AssertionError\n",
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
            ".venv/lib/test_ignored.py": "def test_ignored():\n    raise AssertionError\n",
        },
    )
    code, body = _grade(tmp_path)
    names = {case["name"] for case in body["cases"] if case["verdict"] == "PASS"}
    assert names == {
        "test_a.test_a",
        "tests.test_plain.test_plain",
        "pkgtests.test_pkg.test_pkg",
        "tests.unit.test_x.test_x",
        "test.deep.check_test.test_suffix",
        "tests.unit.test_a.test_a",
    }, body
    assert (code, body["passed"], body["failed"]) == (0, 6, 0), body


def test_a_test_package_under_a_plain_tests_dir_supports_relative_imports(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            "tests/pkg/__init__.py": "",
            "tests/pkg/data.py": "VALUE = 42\n",
            "tests/pkg/test_rel.py": "import app\nfrom . import data\n\ndef test_rel():\n    assert app.value() == data.VALUE\n",
            "tests/broken_test.py": "import missing_module_xyz\n",
        },
    )
    code, body = _grade(tmp_path)
    assert {"name": "tests.pkg.test_rel.test_rel", "verdict": "PASS", "detail": ""} in body["cases"], body
    assert {"name": "tests.broken_test", "verdict": "FAIL", "detail": "import:ModuleNotFoundError"} in body["cases"], body
    assert code == 1


def test_test_files_created_during_the_run_are_never_graded(tmp_path: Path) -> None:
    """Trusted tests are collected from the baseline snapshot before any candidate code runs."""
    _write(
        tmp_path,
        {
            "app.py": (
                "import pathlib\n"
                "for name in ('test_zz.py', 'tests/test_zz.py', 'tests/deep/zz_test.py'):\n"
                "    path = pathlib.Path(__file__).parent / name\n"
                "    path.parent.mkdir(parents=True, exist_ok=True)\n"
                "    path.write_text('def test_extra():\\n    pass\\n')\n"
                "def value():\n    return 42\n"
            ),
            "test_a.py": "import app\n\ndef test_a():\n    assert app.value() == 42\n",
            "tests/test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert {case["name"] for case in body["cases"]} == {"test_a.test_a", "tests.test_b.test_b"}, body
    assert (code, body["passed"], body["failed"]) == (0, 2, 0), body
