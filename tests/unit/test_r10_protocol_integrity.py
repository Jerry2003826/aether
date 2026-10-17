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


def test_same_named_helpers_in_two_test_dirs_resolve_to_each_tests_own_dir(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            # Plain (non-package) dirs: a/helpers.py and b/helpers.py are both importable as `helpers`.
            "a/helpers.py": "WHICH = 'a'\n",
            "a/test_x.py": "import helpers\n\ndef test_x():\n    assert helpers.WHICH == 'a'\n",
            "b/helpers.py": "WHICH = 'b'\n",
            "b/test_y.py": "import helpers\n\ndef test_y():\n    assert helpers.WHICH == 'b'\n",
            # The same under tests/, where the helpers are grading files rather than candidate code.
            "tests/c/helpers.py": "WHICH = 'c'\n",
            "tests/c/test_c.py": "import helpers\n\ndef test_c():\n    assert helpers.WHICH == 'c'\n",
            "tests/d/helpers.py": "WHICH = 'd'\n",
            "tests/d/test_d.py": "import helpers\n\ndef test_d():\n    assert helpers.WHICH == 'd'\n",
        },
    )
    code, body = _grade(tmp_path)
    verdicts = {case["name"]: case["verdict"] for case in body["cases"]}
    assert verdicts == {
        "a.test_x.test_x": "PASS",
        "b.test_y.test_y": "PASS",
        "tests.c.test_c.test_c": "PASS",
        "tests.d.test_d.test_d": "PASS",
    }, body
    assert (code, body["passed"], body["failed"]) == (0, 4, 0), body


def test_generator_tests_are_rejected_not_passed_unrun(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 0\n",
            # Calling either returns a generator object without running the body, so the failing
            # assertion would never execute.
            "test_gen.py": (
                "import app\n\n"
                "async def test_async_gen():\n    assert app.value() == 42\n    yield\n\n"
                "def test_sync_gen():\n    assert app.value() == 42\n    yield\n\n"
                "def test_plain():\n    assert app.value() == 0\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    cases = {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}
    assert cases["test_gen.test_async_gen"] == ("FAIL", "unsupported_async_generator"), body
    assert cases["test_gen.test_sync_gen"] == ("FAIL", "unsupported_generator"), body
    assert cases["test_gen.test_plain"] == ("PASS", ""), body
    assert (code, body["passed"], body["failed"]) == (1, 1, 2), body


def _verdicts(body: dict) -> dict[str, tuple[str, str]]:
    return {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}


def test_a_failing_method_in_a_test_class_fails_the_suite(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 0\n",
            "test_mod.py": (
                "import app\n\n"
                "def test_top():\n    assert app.value() == 0\n\n"
                "class TestStrict:\n"
                "    def setup_method(self, method):\n        self.expected = 42\n\n"
                "    def test_x(self):\n        assert app.value() == self.expected\n\n"
                "    def test_ok(self):\n        assert self.expected == 42\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    assert _verdicts(body) == {
        "test_mod.test_top": ("PASS", ""),
        "test_mod.TestStrict.test_x": ("FAIL", "AssertionError"),
        "test_mod.TestStrict.test_ok": ("PASS", ""),
    }, body
    assert (code, body["passed"], body["failed"]) == (1, 2, 1), body


def test_unittest_testcase_methods_run_with_setup_and_teardown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 0\n",
            "test_case.py": (
                "import unittest\n"
                "import app\n\n"
                "class StrictCase(unittest.TestCase):\n"
                "    def setUp(self):\n        self.expected = 42\n\n"
                "    def tearDown(self):\n        if getattr(self, 'poison', False):\n            raise RuntimeError('teardown')\n\n"
                "    def test_value(self):\n        self.assertEqual(app.value(), self.expected)\n\n"
                "    def test_setup_ran(self):\n        self.assertEqual(self.expected, 42)\n\n"
                "    def test_teardown_fails(self):\n        self.poison = True\n\n"
                "    @unittest.skip('later')\n"
                "    def test_skipped(self):\n        pass\n\n"
                "    @unittest.expectedFailure\n"
                "    def test_expected_failure(self):\n        self.assertEqual(1, 2)\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    verdicts = _verdicts(body)
    assert verdicts["test_case.StrictCase.test_value"] == ("FAIL", "AssertionError"), body
    assert verdicts["test_case.StrictCase.test_setup_ran"] == ("PASS", ""), body
    assert verdicts["test_case.StrictCase.test_teardown_fails"] == ("FAIL", "RuntimeError"), body
    # Only a real pass counts: a skip or an expected failure is never a PASS.
    assert verdicts["test_case.StrictCase.test_skipped"] == ("FAIL", "skipped"), body
    assert verdicts["test_case.StrictCase.test_expected_failure"] == ("FAIL", "expected_failure"), body
    assert (code, body["passed"]) == (1, 1), body


def test_test_classes_the_grader_cannot_run_fail_explicitly(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 0\n",
            "test_odd.py": (
                "def test_top():\n    assert True\n\n"
                "class TestWithInit:\n"
                "    def __init__(self, value):\n        self.value = value\n\n"
                "    def test_value(self):\n        assert self.value == 1\n\n"
                "class TestOuter:\n"
                "    class TestInner:\n"
                "        def test_hidden(self):\n            assert False\n\n"
                "    def test_outer(self):\n        assert True\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    verdicts = _verdicts(body)
    assert verdicts["test_odd.test_top"] == ("PASS", ""), body
    assert verdicts["test_odd.TestWithInit"] == ("FAIL", "unsupported_test_class:init"), body
    assert verdicts["test_odd.TestOuter.TestInner"] == ("FAIL", "unsupported_test_class:nested"), body
    assert verdicts["test_odd.TestOuter.test_outer"] == ("PASS", ""), body
    assert code == 1 and body["failed"] == 2, body


def test_inherited_test_methods_are_collected_for_each_class(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 0\n",
            "test_inherit.py": (
                "import app\n\n"
                "class TestBase:\n"
                "    expected = 0\n"
                "    def test_value(self):\n        assert app.value() == self.expected\n\n"
                "class TestStricter(TestBase):\n"
                "    expected = 42\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    assert _verdicts(body) == {
        "test_inherit.TestBase.test_value": ("PASS", ""),
        "test_inherit.TestStricter.test_value": ("FAIL", "AssertionError"),
    }, body
    assert code == 1, body


def test_class_and_module_fixtures_run_so_a_correct_solution_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            "test_case_fixtures.py": (
                "import unittest\nimport app\n\n"
                "EXPECTED = {}\n\n"
                "def setUpModule():\n    EXPECTED['module'] = 42\n\n"
                "class ValueCase(unittest.TestCase):\n"
                "    @classmethod\n"
                "    def setUpClass(cls):\n        cls.expected = 42\n\n"
                "    def test_class_fixture(self):\n        self.assertEqual(app.value(), self.expected)\n\n"
                "    def test_module_fixture(self):\n        self.assertEqual(app.value(), EXPECTED['module'])\n"
            ),
            "test_plain_fixtures.py": (
                "import app\n\n"
                "STATE = {}\n\n"
                "def setup_module(module):\n    STATE['expected'] = 42\n\n"
                "def test_module_fixture():\n    assert app.value() == STATE['expected']\n\n"
                "class TestValue:\n"
                "    @classmethod\n"
                "    def setup_class(cls):\n        cls.expected = 42\n\n"
                "    def test_class_fixture(self):\n        assert app.value() == self.expected\n\n"
                "    def test_module_fixture(self):\n        assert app.value() == STATE['expected']\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    assert _verdicts(body) == {
        "test_case_fixtures.ValueCase.test_class_fixture": ("PASS", ""),
        "test_case_fixtures.ValueCase.test_module_fixture": ("PASS", ""),
        "test_plain_fixtures.test_module_fixture": ("PASS", ""),
        "test_plain_fixtures.TestValue.test_class_fixture": ("PASS", ""),
        "test_plain_fixtures.TestValue.test_module_fixture": ("PASS", ""),
    }, body
    assert (code, body["passed"], body["failed"]) == (0, 5, 0), body


def test_a_failing_class_or_module_fixture_fails_the_test(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            "test_broken_fixtures.py": (
                "import unittest\nimport app\n\n"
                "class BrokenClass(unittest.TestCase):\n"
                "    @classmethod\n"
                "    def setUpClass(cls):\n        raise RuntimeError('class fixture')\n\n"
                "    def test_value(self):\n        self.assertEqual(app.value(), 42)\n\n"
                "class BrokenTeardown(unittest.TestCase):\n"
                "    @classmethod\n"
                "    def tearDownClass(cls):\n        raise RuntimeError('class teardown')\n\n"
                "    def test_value(self):\n        self.assertEqual(app.value(), 42)\n"
            ),
            "test_broken_module.py": (
                "import app\n\n"
                "def teardown_module(module):\n    raise RuntimeError('module teardown')\n\n"
                "def test_value():\n    assert app.value() == 42\n"
            ),
        },
    )
    code, body = _grade(tmp_path)
    assert _verdicts(body) == {
        "test_broken_fixtures.BrokenClass.test_value": ("FAIL", "RuntimeError"),
        "test_broken_fixtures.BrokenTeardown.test_value": ("FAIL", "RuntimeError"),
        "test_broken_module.test_value": ("FAIL", "RuntimeError"),
    }, body
    assert code == 1, body
