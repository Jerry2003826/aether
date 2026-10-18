"""A candidate's builtin-subclass instance keeps its builtin ancestry in the test (Codex r4308421630).

The reverse of r4308195944: a `class Result(list)` instance (or namedtuple, IntEnum member, Counter)
returned by candidate code is a handle in the test process, and `isinstance(x, list)` was False, so a
trusted `assert not isinstance(app.result(), list)` passed against a candidate it fails in-process.
Now `x.__class__` is a stand-in built in the test process from the trusted classes of the candidate
class's MRO (the builtin data types, OrderedDict, defaultdict, Counter and the enum bases, each checked
by identity against the copy the protocol imported before any candidate code ran); isinstance and
issubclass against those, and the ABCs they are registered with, answer as in-process. A class whose
MRO holds any other class that is not the candidate's own (a stdlib or library class, or one that
claims a stdlib module) is refused as candidate_object_unsupported:subclass_ancestry_unavailable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-subclass-ancestry"

_APP = '''
import collections
import enum
import http


class Result(list):
    pass


Point = collections.namedtuple("Point", "x y")


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Tally(collections.Counter):
    pass


class Ordered(collections.OrderedDict):
    pass


class Fake(dict):
    __module__ = "collections"
    __qualname__ = "Counter"


def result():
    return Result([1, 2])


def point():
    return Point(1, 2)


def counts():
    return collections.Counter("aab")


def tally():
    return Tally("ab")


def ordered():
    return Ordered(a=1)


def fake():
    return Fake(a=1)


def status():
    return http.HTTPStatus.OK


def length(items):
    return len(items)
'''

_TESTS = '''
import collections
import collections.abc
import enum

import app


def test_a_list_subclass_is_a_list():
    assert isinstance(app.result(), list)


def test_a_list_subclass_cannot_pass_as_not_a_list():
    assert not isinstance(app.result(), list)


def test_a_list_subclass_is_not_a_tuple():
    assert not isinstance(app.result(), (tuple, dict, str))


def test_a_list_subclass_is_a_mutable_sequence():
    assert isinstance(app.result(), collections.abc.MutableSequence)


def test_its_class_is_a_list_subclass_named_as_the_candidate_class():
    kind = app.result().__class__
    assert issubclass(kind, list) and kind is not list and kind.__name__ == "Result"


def test_a_namedtuple_is_a_tuple():
    assert isinstance(app.point(), tuple)


def test_an_int_enum_member_is_an_int_and_an_enum():
    member = app.Level.HIGH
    assert isinstance(member, int) and isinstance(member, enum.IntEnum) and isinstance(member, enum.Enum)


def test_a_counter_is_a_counter_and_a_dict():
    assert isinstance(app.counts(), collections.Counter) and isinstance(app.counts(), dict)


def test_a_counter_subclass_is_a_counter():
    assert isinstance(app.tally(), collections.Counter)


def test_an_ordered_dict_subclass_is_an_ordered_dict():
    assert isinstance(app.ordered(), collections.OrderedDict)


def test_the_handle_still_goes_back_to_candidate_code():
    assert app.length(app.result()) == 2


def test_a_class_claiming_a_stdlib_name_is_refused():
    assert not isinstance(app.fake(), collections.Counter)


def test_a_stdlib_enum_outside_the_trusted_set_is_refused():
    assert isinstance(app.status(), int)
'''

_REASON = "candidate_object_unsupported:subclass_ancestry_unavailable"
_IN_PROCESS = {
    "test_a_list_subclass_is_a_list": "PASS",
    "test_a_list_subclass_cannot_pass_as_not_a_list": "FAIL",
    "test_a_list_subclass_is_not_a_tuple": "PASS",
    "test_a_list_subclass_is_a_mutable_sequence": "PASS",
    "test_its_class_is_a_list_subclass_named_as_the_candidate_class": "PASS",
    "test_a_namedtuple_is_a_tuple": "PASS",
    "test_an_int_enum_member_is_an_int_and_an_enum": "PASS",
    "test_a_counter_is_a_counter_and_a_dict": "PASS",
    "test_a_counter_subclass_is_a_counter": "PASS",
    "test_an_ordered_dict_subclass_is_an_ordered_dict": "PASS",
    "test_the_handle_still_goes_back_to_candidate_code": "PASS",
    "test_a_class_claiming_a_stdlib_name_is_refused": "PASS",
    "test_a_stdlib_enum_outside_the_trusted_set_is_refused": "PASS",
}
_REFUSED = {"test_a_class_claiming_a_stdlib_name_is_refused", "test_a_stdlib_enum_outside_the_trusted_set_is_refused"}


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
def test_builtin_ancestry_is_kept_or_refused_by_name(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
    for name in _REFUSED:
        assert cases[name][1] == _REASON, (name, cases[name], output[-4000:])
