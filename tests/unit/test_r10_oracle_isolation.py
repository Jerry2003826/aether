"""Candidate code cannot change the trusted verdict: oracle values, pass/fail, or the reported result.

The protocol prints its result on one line carrying a nonce it read (and deleted) from
AETHER_PROTOCOL_NONCE_FILE before any candidate code ran. Only that line counts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "n0nce-3f9a1c2b7d4e5f60"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _grade(root: Path) -> tuple[int, dict | None, str]:
    """(exit code, the verified result body or None, raw output)."""
    secret = root.parent / (root.name + ".nonce")
    secret.write_text(_NONCE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env["AETHER_PROTOCOL_NONCE_FILE"] = str(secret)
    env["AETHER_PROTOCOL_SHM_DIR"] = "/dev/shm"  # required by the protocol; the entry names the emptied one
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = []
    for line in completed.stdout.splitlines():
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if isinstance(body, dict) and "cases" in body:
            lines.append(body)
    signed = [body for body in lines if body.get("nonce") == _NONCE]
    # Exactly one line signed with the nonce is the result. (Before the nonce existed, the last JSON
    # line was the result; that is what a reader would have used, so the verdict asserts run on it.)
    verified = signed[0] if len(signed) == 1 else (lines[-1] if lines and not signed else None)
    return completed.returncode, verified, completed.stdout + completed.stderr + f"\nnonce_file_left={secret.exists()}"


def _assert_failed(code: int, body: dict | None, output: str, case: str) -> None:
    assert body is not None, "no single verified result line: " + output
    assert code == 1, (code, body)
    assert body["failed"] >= 1, body
    assert not any(item["name"] == case and item["verdict"] == "PASS" for item in body["cases"]), body
    _assert_signed(body, output)


def _assert_signed(body: dict, output: str) -> None:
    assert body.get("nonce") == _NONCE, "the result line is not signed with the nonce: " + output
    assert output.endswith("nonce_file_left=False"), "the nonce file must be deleted before candidate code runs"


_ORACLE = "def expected():\n    return 42\n"
_TEST = "import app\nimport oracle\n\ndef test_value():\n    assert app.value() == oracle.expected()\n"


def test_candidate_cannot_monkeypatch_the_oracle(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "import oracle\noracle.expected = lambda: 0\n\ndef value():\n    return 0\n",
            "oracle.py": _ORACLE,
            "test_value.py": _TEST,
        },
    )
    _assert_failed(*_grade(tmp_path), "test_value.test_value")


def test_candidate_cannot_patch_the_oracle_through_sys_modules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import sys\n"
                "def value():\n"
                "    sys.modules['oracle'].expected = lambda: 0\n"
                "    return 0\n"
            ),
            "oracle.py": _ORACLE,
            # The oracle is imported first, so the patch would land before the comparison.
            "test_value.py": "import oracle\nimport app\n\ndef test_value():\n    got = app.value()\n    assert got == oracle.expected()\n",
        },
    )
    _assert_failed(*_grade(tmp_path), "test_value.test_value")


def test_candidate_cannot_rewrite_the_recorded_result(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            # Rewrites the result dict in place when it is serialised, which also flips the exit code.
            "app.py": (
                "import json\n"
                "_dumps = json.dumps\n"
                "def _forge(body, *args, **kwargs):\n"
                "    if isinstance(body, dict) and 'cases' in body:\n"
                "        body['cases'] = [dict(case, verdict='PASS', detail='') for case in body['cases']]\n"
                "        body['passed'], body['failed'] = len(body['cases']), 0\n"
                "    return _dumps(body, *args, **kwargs)\n"
                "json.dumps = _forge\n"
                "def value():\n    return 0\n"
            ),
            "oracle.py": _ORACLE,
            "test_value.py": _TEST,
        },
    )
    _assert_failed(*_grade(tmp_path), "test_value.test_value")


def test_candidate_cannot_forge_the_result_line_and_exit(tmp_path: Path) -> None:
    forged = {"passed": 1, "failed": 0, "errors": 0, "cases": [{"name": "test_value.test_value", "verdict": "PASS", "detail": ""}]}
    _write(
        tmp_path,
        {
            "app.py": (
                "import json, os, sys\n"
                f"forged = {forged!r}\n"
                "nonce = ''\n"
                "# Every way a same-uid process might learn the nonce or reach the real stdout.\n"
                "path = os.environ.get('AETHER_PROTOCOL_NONCE_FILE')\n"
                "for source in [path, f'/proc/{os.getppid()}/environ', f'/proc/{os.getppid()}/cmdline']:\n"
                "    try:\n"
                "        text = open(source, 'rb').read().decode('utf-8', 'replace') if source else ''\n"
                "    except OSError:\n"
                "        continue\n"
                "    for piece in text.replace('\\x00', '\\n').splitlines():\n"
                "        if piece.startswith('n0nce-') or (source == path and piece):\n"
                "            nonce = piece.split('=', 1)[-1]\n"
                "if nonce:\n"
                "    forged['nonce'] = nonce\n"
                "line = json.dumps(forged) + '\\n'\n"
                "# One forged line: straight into the grader's stdout if reachable, else our own stdout.\n"
                "try:\n"
                "    with open(f'/proc/{os.getppid()}/fd/1', 'w') as out:\n"
                "        out.write(line)\n"
                "except OSError:\n"
                "    sys.stdout.write(line)\n"
                "    sys.stdout.flush()\n"
                "os._exit(0)\n"
            ),
            "oracle.py": _ORACLE,
            "test_value.py": _TEST,
        },
    )
    _assert_failed(*_grade(tmp_path), "test_value.test_value")


def test_an_object_that_equals_everything_does_not_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "class Anything:\n"
                "    def __eq__(self, other):\n        return True\n"
                "    def __ne__(self, other):\n        return False\n"
                "    def __bool__(self):\n        return True\n"
                "    __hash__ = object.__hash__\n"
                "def value():\n    return Anything()\n"
            ),
            "oracle.py": _ORACLE,
            "test_value.py": _TEST,
        },
    )
    _assert_failed(*_grade(tmp_path), "test_value.test_value")


def test_honest_candidates_still_work_through_the_isolation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg.core import Counter, pairs\n",
            "pkg/core.py": (
                "class Counter:\n"
                "    def __init__(self, start=0):\n        self.count = start\n"
                "    def bump(self, by=1):\n        self.count += by\n        return self.count\n"
                "def pairs(items):\n    return {item: (item, len(item)) for item in items}\n"
            ),
            "app.py": (
                "import math\n"
                "LIMIT = 3\n"
                "print('candidate output is fine')\n"
                "def value():\n    return 42\n"
                "def data():\n    return [1, 2.5, None, True, 'x', b'\\x00\\xff', (1, 2), {'k': [1]}, {3, 4}, float('inf')]\n"
                "def nan():\n    return math.nan\n"
                "def limited(n):\n    if n > LIMIT:\n        raise ValueError('too big')\n    return n\n"
                "def gen(n):\n    yield from range(n)\n"
                "def keys():\n    return {1: 'a', (2, 3): 'b'}\n"
            ),
            "oracle.py": _ORACLE,
            "test_value.py": (
                "import math\n"
                "import app\n"
                "import oracle\n"
                "from pkg import Counter, pairs\n\n"
                "def test_value():\n    assert app.value() == oracle.expected()\n\n"
                "def test_data():\n    assert app.data() == [1, 2.5, None, True, 'x', b'\\x00\\xff', (1, 2), {'k': [1]}, {3, 4}, float('inf')]\n"
                "    assert math.isnan(app.nan())\n"
                "    assert app.keys() == {1: 'a', (2, 3): 'b'}\n\n"
                "def test_errors_and_module_state():\n"
                "    try:\n        app.limited(5)\n    except ValueError as exc:\n        assert 'too big' in str(exc)\n"
                "    else:\n        raise AssertionError('no error')\n"
                "    app.LIMIT = 10\n"
                "    assert app.LIMIT == 10\n"
                "    assert app.limited(5) == 5\n\n"
                "def test_objects_and_iterators():\n"
                "    counter = Counter(start=1)\n"
                "    assert counter.bump() == 2\n"
                "    assert counter.bump(by=3) == 5\n"
                "    assert counter.count == 5\n"
                "    assert list(app.gen(3)) == [0, 1, 2]\n"
                "    assert pairs(['ab']) == {'ab': ('ab', 2)}\n"
            ),
        },
    )
    code, body, output = _grade(tmp_path)
    assert body is not None, output
    assert (code, body["passed"], body["failed"]) == (0, 4, 0), body
    _assert_signed(body, output)


# Candidate code walks the caller's frames: every test_* it can see becomes a passing no-op, by
# replacing the name in the caller's globals, rewriting the function's __code__, and patching every
# test module in sys.modules; the oracle it finds there is patched too.
_FRAME_WALK = (
    "import inspect, sys\n"
    "def _noop():\n    return None\n"
    "def _swap(scope):\n"
    "    for name, item in list(scope.items()):\n"
    "        if name.startswith('test_') and callable(item):\n"
    "            try:\n                item.__code__ = _noop.__code__\n            except (AttributeError, TypeError, ValueError):\n                pass\n"
    "            scope[name] = lambda: None\n"
    "        if name == 'oracle':\n"
    "            try:\n                item.expected = lambda: 0\n            except Exception:\n                pass\n"
    "def helper():\n"
    "    frame = inspect.currentframe().f_back\n"
    "    while frame is not None:\n"
    "        _swap(frame.f_globals)\n"
    "        frame = frame.f_back\n"
    "    for module in list(sys.modules.values()):\n"
    "        if getattr(module, '__name__', '').startswith('test_'):\n"
    "            _swap(vars(module))\n"
    "    # Test modules imported later are swapped as they load.\n"
    "    import importlib\n"
    "    _import = importlib.import_module\n"
    "    def _patched(name, *args, **kwargs):\n"
    "        module = _import(name, *args, **kwargs)\n"
    "        _swap(vars(module))\n"
    "        return module\n"
    "    importlib.import_module = _patched\n"
    "    return 1\n"
    "def value():\n    return 0\n"
)


def test_an_earlier_test_cannot_swap_a_later_test_in_the_same_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": _FRAME_WALK,
            "oracle.py": _ORACLE,
            # test_a runs first (tests run in name order) and calls the candidate's helper.
            "test_value.py": (
                "import app\nimport oracle\n\n"
                "def test_a_helper():\n    assert app.helper() == 1\n\n"
                "def test_b_value():\n    assert app.value() == oracle.expected()\n"
            ),
        },
    )
    code, body, output = _grade(tmp_path)
    _assert_failed(code, body, output, "test_value.test_b_value")
    assert {"name": "test_value.test_b_value", "verdict": "FAIL"}.items() <= next(
        item for item in body["cases"] if item["name"] == "test_value.test_b_value"
    ).items(), body


def test_a_test_cannot_swap_a_test_in_a_later_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": _FRAME_WALK,
            "oracle.py": _ORACLE,
            "test_a.py": "import app\n\ndef test_helper():\n    assert app.helper() == 1\n",
            "test_b.py": _TEST,
        },
    )
    _assert_failed(*_grade(tmp_path), "test_b.test_value")


def test_the_oracle_found_through_the_callers_frame_is_not_patched(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import inspect\n"
                "def value():\n"
                "    frame = inspect.currentframe().f_back\n"
                "    while frame is not None:\n"
                "        for item in list(frame.f_globals.values()) + list(frame.f_locals.values()):\n"
                "            if getattr(item, '__name__', '') == 'oracle':\n"
                "                item.expected = lambda: 0\n"
                "        frame = frame.f_back\n"
                "    return 0\n"
            ),
            "oracle.py": _ORACLE,
            "test_value.py": "import oracle\nimport app\n\ndef test_value():\n    got = app.value()\n    assert got == oracle.expected()\n",
        },
    )
    _assert_failed(*_grade(tmp_path), "test_value.test_value")


def test_candidate_cannot_forge_a_result_on_any_inherited_descriptor(tmp_path: Path) -> None:
    forged = {"name": "test_value.test_value", "verdict": "PASS", "detail": ""}
    _write(
        tmp_path,
        {
            "app.py": (
                "import json, os, struct\n"
                f"forged = {forged!r}\n"
                "def value():\n"
                "    line = json.dumps(forged).encode()\n"
                "    framed = struct.pack('>I', len(line)) + line\n"
                "    for fd in range(3, 256):\n"
                "        for data in (framed, line + b'\\n'):\n"
                "            try:\n                os.write(fd, data)\n            except OSError:\n                pass\n"
                "    os._exit(0)\n"
            ),
            "oracle.py": _ORACLE,
            "test_value.py": _TEST,
        },
    )
    _assert_failed(*_grade(tmp_path), "test_value.test_value")


def test_a_test_whose_process_dies_fails_and_every_collected_test_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import os, signal\n"
                "def value():\n    return 42\n"
                "def crash():\n"
                "    os.kill(os.getpid(), signal.SIGKILL)\n"
                "    os._exit(0)\n"
            ),
            "oracle.py": _ORACLE,
            "test_value.py": (
                "import app\nimport oracle\n\n"
                "def test_a():\n    assert app.value() == oracle.expected()\n\n"
                "def test_b_crash():\n    app.crash()\n\n"
                "def test_c():\n    assert app.value() == oracle.expected()\n"
            ),
        },
    )
    code, body, output = _grade(tmp_path)
    _assert_failed(code, body, output, "test_value.test_b_crash")
    verdicts = {item["name"]: item["verdict"] for item in body["cases"]}
    assert verdicts.get("test_value.test_b_crash") == "FAIL", body
    assert verdicts.get("test_value.test_a") == "PASS", body
    assert verdicts.get("test_value.test_c") == "PASS", body


def _unsupported(root: Path, files: dict[str, str], case: str, reason: str) -> None:
    _write(root, files)
    code, body, output = _grade(root)
    _assert_failed(code, body, output, case)
    detail = next(item["detail"] for item in body["cases"] if item["name"] == case)
    assert detail == "candidate_object_unsupported:" + reason, body


def test_isinstance_against_a_candidate_class_is_reported_as_unsupported(tmp_path: Path) -> None:
    _unsupported(
        tmp_path,
        {
            "app.py": "class Box:\n    pass\ndef make():\n    return Box()\n",
            "test_box.py": "import app\n\ndef test_box():\n    assert isinstance(app.make(), app.Box)\n",
        },
        "test_box.test_box",
        "isinstance_candidate_class",
    )


def test_catching_a_candidate_exception_class_is_reported_as_unsupported(tmp_path: Path) -> None:
    _unsupported(
        tmp_path,
        {
            "app.py": "class Invalid(Exception):\n    pass\ndef check(n):\n    raise Invalid(str(n))\n",
            "test_invalid.py": (
                "import app\n\n"
                "def test_invalid():\n"
                "    try:\n        app.check(-1)\n    except app.Invalid:\n        pass\n"
                "    else:\n        raise AssertionError('no error')\n"
            ),
        },
        "test_invalid.test_invalid",
        "except_candidate_exception_class",
    )


def test_passing_a_callback_into_candidate_code_is_reported_as_unsupported(tmp_path: Path) -> None:
    _unsupported(
        tmp_path,
        {
            "app.py": "def apply(fn, value):\n    return fn(value)\n",
            "test_apply.py": "import app\n\ndef test_apply():\n    assert app.apply(lambda n: n + 1, 1) == 2\n",
        },
        "test_apply.test_apply",
        "callable_passed_to_candidate",
    )


# A test that swallows the unsupported-object error must not pass without candidate code running.
_APPLY = "def apply(fn, value):\n    return fn(value)\n"


def test_swallowing_the_unsupported_error_with_except_typeerror_still_fails(tmp_path: Path) -> None:
    _unsupported(
        tmp_path,
        {
            "app.py": _APPLY,
            "test_apply.py": (
                "import app\n\n"
                "def test_apply():\n"
                "    try:\n        app.apply(lambda n: n + 1, 1)\n    except TypeError:\n        pass\n"
            ),
        },
        "test_apply.test_apply",
        "callable_passed_to_candidate",
    )


def test_swallowing_the_unsupported_error_with_except_exception_still_fails(tmp_path: Path) -> None:
    _unsupported(
        tmp_path,
        {
            "app.py": "class Box:\n    pass\ndef make():\n    return Box()\n",
            "test_box.py": (
                "import app\n\n"
                "def test_box():\n"
                "    try:\n        assert not isinstance(app.make(), app.Box)\n    except Exception:\n        pass\n"
            ),
        },
        "test_box.test_box",
        "isinstance_candidate_class",
    )


def test_swallowing_the_unsupported_error_with_pytest_raises_still_fails(tmp_path: Path) -> None:
    _unsupported(
        tmp_path,
        {
            "app.py": _APPLY,
            "test_apply.py": (
                "import pytest\nimport app\n\n"
                "def test_apply():\n"
                "    with pytest.raises(TypeError):\n        app.apply(lambda n: n + 1, 1)\n"
            ),
        },
        "test_apply.test_apply",
        "callable_passed_to_candidate",
    )


def test_swallowing_the_unsupported_error_in_a_testcase_still_fails(tmp_path: Path) -> None:
    _unsupported(
        tmp_path,
        {
            "app.py": _APPLY,
            "test_apply.py": (
                "import unittest\nimport app\n\n"
                "class ApplyCase(unittest.TestCase):\n"
                "    def test_apply(self):\n"
                "        with self.assertRaises(TypeError):\n            app.apply(lambda n: n + 1, 1)\n"
            ),
        },
        "test_apply.ApplyCase.test_apply",
        "callable_passed_to_candidate",
    )
