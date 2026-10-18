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

import pytest

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-pytest-collection-tests"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _grade(root: Path, **extra: str) -> tuple[int, dict[str, tuple[str, str]], str]:
    """(exit code, {case name: (verdict, detail)} from the nonce-signed line, raw output)."""
    secret = root.parent / (root.name + ".nonce")
    secret.write_text(_NONCE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env["AETHER_PROTOCOL_NONCE_FILE"] = str(secret)
    env["AETHER_PROTOCOL_SHM_DIR"] = "/dev/shm"
    env.update(extra)
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


@pytest.mark.parametrize(
    ("relative", "label"),
    [
        ("test_bad-file.py", "test_bad-file"),
        ("tests/test_bad-file.py", "tests.test_bad-file"),
        ("checks/bad-file_test.py", "checks.bad-file_test"),
    ],
)
def test_a_test_file_whose_name_is_not_an_identifier_is_collected_and_can_fail_the_suite(
    tmp_path: Path, relative: str, label: str
) -> None:
    # pytest collects test_bad-file.py like any test_*.py; a passing module elsewhere must not hide it.
    _write(
        tmp_path,
        {
            "app.py": _APP,
            "test_ok.py": "import app\n\ndef test_ok():\n    assert app.value() == 1\n",
            relative: (
                "import app\n\n"
                "def test_good():\n    assert app.value() == 1\n\n"
                "def test_bad():\n    assert app.value() == 2\n"
            ),
        },
    )
    code, verdicts, output = _grade(tmp_path)
    assert verdicts == {
        "test_ok.test_ok": ("PASS", ""),
        f"{label}.test_good": ("PASS", ""),
        f"{label}.test_bad": ("FAIL", "AssertionError"),
    }, output
    assert code == 1, output


# A stack whose pop() does not remove: each test alone passes; run as one class, as pytest and
# unittest do, the second test sees what the first one left and fails.
_STACK = (
    "class Stack:\n"
    "    def __init__(self):\n        self.items = []\n"
    "    def push(self, item):\n        self.items.append(item)\n"
    "    def pop(self):\n        return self.items[-1]\n"
    "    def size(self):\n        return len(self.items)\n"
    "_TAGS = iter(['x'] * 100)\n"
    "def tag():\n    return next(_TAGS)\n"
)
_CLASS_SUITES = {
    "unittest_setupclass": (
        ("import unittest\nimport app\n\n"
        "class StackTests(unittest.TestCase):\n"
        "    @classmethod\n    def setUpClass(cls):\n        cls.stack = app.Stack()\n\n"
        "    def test_a_push_pop(self):\n        self.stack.push(1)\n        self.assertEqual(self.stack.pop(), 1)\n\n"
        "    def test_b_empty_afterwards(self):\n        self.assertEqual(self.stack.size(), 0)\n"),
        {"StackTests.test_a_push_pop": ("PASS", ""), "StackTests.test_b_empty_afterwards": ("FAIL", "AssertionError")},
    ),
    "unittest_teardownclass": (
        # tearDownClass checks what the whole class saw: two tags, each different.
        ("import unittest\nimport app\n\n"
        "class TagTests(unittest.TestCase):\n"
        "    seen = []\n\n"
        "    def test_one(self):\n        self.seen.append(app.tag())\n\n"
        "    def test_two(self):\n        self.seen.append(app.tag())\n\n"
        "    @classmethod\n    def tearDownClass(cls):\n        assert len(set(cls.seen)) == 2, cls.seen\n"),
        {"TagTests.test_one": ("PASS", ""), "TagTests.test_two": ("FAIL", "AssertionError")},
    ),
    "pytest_setup_class": (
        ("import app\n\n"
        "class TestStack:\n"
        "    @classmethod\n    def setup_class(cls):\n        cls.stack = app.Stack()\n\n"
        "    def test_a_push_pop(self):\n        self.stack.push(1)\n        assert self.stack.pop() == 1\n\n"
        "    def test_b_empty_afterwards(self):\n        assert self.stack.size() == 0\n"),
        {"TestStack.test_a_push_pop": ("PASS", ""), "TestStack.test_b_empty_afterwards": ("FAIL", "AssertionError")},
    ),
}


@pytest.mark.parametrize("suite", sorted(_CLASS_SUITES))
def test_class_fixtures_run_once_around_the_whole_class(tmp_path: Path, suite: str) -> None:
    source, expected = _CLASS_SUITES[suite]
    _write(tmp_path, {"app.py": _STACK, "test_cls.py": source})
    code, verdicts, output = _grade(tmp_path)
    assert verdicts == {f"test_cls.{name}": verdict for name, verdict in expected.items()}, output
    assert code == 1, output


@pytest.mark.parametrize("package", ["pkg", "my-pkg"])
def test_pytest_never_imports_a_package_init_in_the_test_process(tmp_path: Path, package: str) -> None:
    # <package>/__init__.py is candidate code. pytest's Package node would import it in the (unlocked)
    # test process; `my-pkg` is no import name, so nothing but pytest's own path import could load it.
    # It may only ever run in the Landlocked candidate process, which cannot write the marker.
    marker = tmp_path.parent / (tmp_path.name + ".marker")
    _write(
        tmp_path,
        {
            "app.py": _APP,
            f"{package}/__init__.py": (
                "try:\n"
                f"    open({str(marker)!r}, 'w').write('ran unlocked')\n"
                "except OSError:\n"
                "    pass\n"
            ),
            f"{package}/test_inside.py": "import app\n\ndef test_value():\n    assert app.value() == 1\n",
        },
    )
    code, verdicts, output = _grade(tmp_path)
    assert verdicts == {f"{package}.test_inside.test_value": ("PASS", "")}, output
    assert code == 0, output
    assert not marker.exists(), output


def test_a_hung_test_fails_alone_and_the_rest_of_its_class_still_runs(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": _APP,
            "test_hang.py": (
                "import time\nimport app\n\n"
                "class TestSeq:\n"
                "    def test_a(self):\n        assert app.value() == 1\n\n"
                "    def test_b_hangs(self):\n        time.sleep(60)\n\n"
                "    def test_c(self):\n        assert app.value() == 1\n\n"
                "    def test_d_fails(self):\n        assert app.value() == 2\n"
            ),
        },
    )
    code, verdicts, output = _grade(tmp_path, AETHER_PROTOCOL_TEST_TIMEOUT_SECONDS="3")
    assert verdicts == {
        "test_hang.TestSeq.test_a": ("PASS", ""),
        "test_hang.TestSeq.test_b_hangs": ("FAIL", "timeout"),
        "test_hang.TestSeq.test_c": ("PASS", ""),
        "test_hang.TestSeq.test_d_fails": ("FAIL", "AssertionError"),
    }, output
    assert code == 1, output


_CONFIGURED_APP = "CALLS = []\n\ndef configure(tag):\n    CALLS.append(tag)\n\ndef calls():\n    return list(CALLS)\n"


@pytest.mark.parametrize(
    ("setup", "teardown"),
    [("setup_module(module)", "teardown_module(module)"), ("setUpModule()", "tearDownModule()")],
    ids=["pytest_xunit", "unittest"],
)
def test_module_setup_and_teardown_run_once_around_all_of_the_modules_tests_and_keep_state(
    tmp_path: Path, setup: str, teardown: str
) -> None:
    """Codex 16:41 (coding_protocol.py:1160): module-level setup and teardown run once for the whole module,
    as under pytest, and the module state they build (trusted globals and the candidate's own state) is
    still there for every test of the module, not rebuilt or lost per test."""
    work = tmp_path / "work"
    marks = tmp_path / "marks.txt"
    _write(
        work,
        {
            "app.py": _CONFIGURED_APP,
            "test_mod.py": (
                "import app\n\n"
                "STATE = []\n\n"
                f"def {setup}:\n"
                "    STATE.append('setup')\n"
                "    app.configure('module')\n"
                f"    open({str(marks)!r}, 'a').write('s')\n\n"
                f"def {teardown}:\n"
                f"    open({str(marks)!r}, 'a').write('t')\n\n"
                "def test_first():\n"
                "    assert STATE == ['setup']\n"
                "    assert app.calls() == ['module']\n"
                "    app.configure('first')\n\n"
                "def test_second():\n"
                "    assert STATE == ['setup']\n"
                "    assert app.calls() == ['module', 'first']\n"
            ),
        },
    )
    code, verdicts, output = _grade(work)
    assert verdicts == {"test_mod.test_first": ("PASS", ""), "test_mod.test_second": ("PASS", "")}, output
    assert code == 0, output
    assert marks.read_text(encoding="utf-8") == "st", output
