"""A builtin subclass a trusted test passes into candidate code is refused, never narrowed to its base (Codex r4308195944).

The trusted encoder sent any int/str/tuple/list/dict... instance as the plain builtin, so an IntEnum, a
namedtuple, an OrderedDict or a `class Items(list)` reached candidate code as int/tuple/dict/list: its
type and overrides were gone, and a candidate that branches on `type(arg)` passed through the proxy
while failing the same test in-process. The test's classes cannot enter the candidate process, so such
a value is unsupported (`subclass_passed_to_candidate`, it can never pass), at any depth; exact builtin
values still cross as data.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-subclass-into-candidate"

_APP = '''
def kind(value):
    return type(value).__name__


def is_plain_int(value):
    return type(value) is int


def first_name(value):
    return value.x


def size(value):
    return len(value)


def kinds(values):
    return [type(item).__name__ for item in values]
'''

_TESTS = '''
import enum
from collections import OrderedDict, namedtuple

import app


class Level(enum.IntEnum):
    HIGH = 2


Point = namedtuple("Point", "x y")


class Items(list):
    def __len__(self):
        return 99


def test_an_int_enum_keeps_its_type():
    assert app.kind(Level.HIGH) == "Level"


def test_an_int_enum_is_not_a_plain_int():
    assert app.is_plain_int(Level.HIGH)


def test_a_named_tuple_keeps_its_fields():
    assert app.first_name(Point(1, 2)) == 1


def test_a_list_subclass_keeps_its_override():
    assert app.size(Items([1])) == 99


def test_a_list_subclass_is_not_measured_as_a_list():
    assert app.size(Items([1])) == 1


def test_an_ordered_dict_keeps_its_type():
    assert app.kind(OrderedDict(a=1)) == "OrderedDict"


def test_a_subclass_inside_a_list():
    assert app.kinds([1, Level.HIGH]) == ["int", "int"]


def test_exact_builtin_values_still_cross():
    assert app.kinds([1, True, 1.5, 1j, "s", b"b", (1,), frozenset({1}), [1], {"a": 1}, {1}, bytearray(b"x"), None]) == [
        "int", "bool", "float", "complex", "str", "bytes", "tuple", "frozenset", "list", "dict", "set", "bytearray", "NoneType"
    ]
'''

_REASON = "candidate_object_unsupported:subclass_passed_to_candidate"
_IN_PROCESS = {
    "test_an_int_enum_keeps_its_type": "PASS",
    "test_an_int_enum_is_not_a_plain_int": "FAIL",
    "test_a_named_tuple_keeps_its_fields": "PASS",
    "test_a_list_subclass_keeps_its_override": "PASS",
    "test_a_list_subclass_is_not_measured_as_a_list": "FAIL",
    "test_an_ordered_dict_keeps_its_type": "PASS",
    "test_a_subclass_inside_a_list": "FAIL",
    "test_exact_builtin_values_still_cross": "PASS",
}
_REFUSED = {name for name in _IN_PROCESS if name != "test_exact_builtin_values_still_cross"}


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text(_APP, encoding="utf-8")
    (work / "test_app.py").write_text(_TESTS, encoding="utf-8")
    return work


def _env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}


def _grade(root: Path) -> tuple[dict[str, tuple[str, str]], str]:
    secret = root.parent / (root.name + ".nonce")
    secret.write_text(_NONCE, encoding="utf-8")
    env = _env()
    env["AETHER_PROTOCOL_NONCE_FILE"] = str(secret)
    env["AETHER_PROTOCOL_SHM_DIR"] = "/dev/shm"
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)], cwd=root, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    output = completed.stdout + completed.stderr
    signed = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{") and _NONCE in line]
    assert len(signed) == 1, output[-4000:]
    cases = {case["name"].removeprefix("test_app."): (case["verdict"], case["detail"]) for case in signed[0]["cases"]}
    return cases, output


def test_the_in_process_run_gives_the_expected_verdicts(tmp_path: Path) -> None:
    found: dict[str, str] = {}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", "test_app.py"],
        cwd=_work(tmp_path), env=_env(), capture_output=True, text=True, timeout=300, check=False,
    )
    for verdict, name in re.findall(r"^(PASSED|FAILED) test_app\.py::(\w+)", completed.stdout, re.MULTILINE):
        found[name] = "PASS" if verdict == "PASSED" else "FAIL"
    assert found == _IN_PROCESS, completed.stdout[-4000:]


def test_builtin_subclasses_passed_in_are_refused_and_exact_types_kept(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
    for name in _REFUSED:
        assert cases[name][1] == _REASON, (name, cases[name], output[-4000:])
