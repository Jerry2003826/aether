"""A candidate class that rewrites __hash__ is refused, never given a hash the test process made up (Codex r4307762550).

hash() of a candidate object cannot be answered by the test process: the candidate's __hash__ is
candidate code and its result an int the test could check. Before, the proxy answered with its own
identity hash whenever the class defined __hash__ (or set it to None by defining only __eq__), so
`hash(x) != 123` passed against a class whose __hash__ returns 123, an unhashable candidate object
went into a set, and two equal frozen dataclasses stayed two set members. Now any such hash is
unsupported (it can never pass). Builtin hashing is kept: a str subclass that does not touch
__hash__ hashes as its data, and a plain candidate object, class or function keeps identity
semantics (the same object is one set member, two objects are two).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-hash-hook"

_APP = '''
from dataclasses import dataclass


class Result(str):
    __hash__ = lambda self: 123  # noqa: E731


class SubResult(Result):
    pass


class EqStr(str):
    def __eq__(self, other):
        return str.__eq__(self, other)


class Plain(str):
    pass


class Node:
    def __hash__(self):
        return 7


class Child(Node):
    pass


class EqNode:
    def __eq__(self, other):
        return self is other


class Thing:
    pass


@dataclass(frozen=True)
class Point:
    x: int


def result():
    return Result("r")


def sub_result():
    return SubResult("r")


def eq_str():
    return EqStr("e")


def plain():
    return Plain("abc")


def node():
    return Node()


def child():
    return Child()


def eq_node():
    return EqNode()


SHARED = Thing()


def shared():
    return SHARED


def fresh():
    return Thing()


def point(x):
    return Point(x)
'''

_TESTS = '''
import app


def test_a_rewritten_hash_of_a_str_subclass_is_not_made_up():
    assert hash(app.result()) != 123


def test_a_rewritten_hash_cannot_pass_either_way():
    assert hash(app.result()) == 123


def test_an_inherited_rewritten_hash_of_a_str_subclass():
    assert hash(app.sub_result()) != 123


def test_an_unhashable_str_subclass_cannot_go_into_a_set():
    assert len({app.eq_str()}) == 1


def test_a_plain_str_subclass_hashes_as_its_data():
    assert hash(app.plain()) == hash("abc")
    assert len({app.plain(), "abc"}) == 1


def test_a_rewritten_hash_of_a_candidate_object_is_not_made_up():
    assert hash(app.node()) != 7


def test_an_inherited_rewritten_hash_of_a_candidate_object():
    assert hash(app.child()) != 7


def test_an_unhashable_candidate_object_cannot_go_into_a_set():
    assert len({app.eq_node()}) == 1


def test_equal_frozen_dataclasses_are_not_two_set_members():
    assert len({app.point(1), app.point(1)}) == 2


def test_a_plain_candidate_object_keeps_identity_semantics():
    assert len({app.shared(), app.shared()}) == 1
    assert len({app.fresh(), app.fresh()}) == 2
    assert {app.shared(): 1}[app.shared()] == 1


def test_classes_and_functions_hash_by_identity():
    assert len({app.Thing, app.Thing, app.fresh, app.fresh}) == 2
'''

_REASON = "candidate_object_unsupported:hash_of_candidate_object"
# Verdict of a plain in-process pytest run, and through the protocol (None: same as in-process).
_IN_PROCESS = {
    "test_a_rewritten_hash_of_a_str_subclass_is_not_made_up": "FAIL",
    "test_a_rewritten_hash_cannot_pass_either_way": "PASS",
    "test_an_inherited_rewritten_hash_of_a_str_subclass": "FAIL",
    "test_an_unhashable_str_subclass_cannot_go_into_a_set": "FAIL",
    "test_a_plain_str_subclass_hashes_as_its_data": "PASS",
    "test_a_rewritten_hash_of_a_candidate_object_is_not_made_up": "FAIL",
    "test_an_inherited_rewritten_hash_of_a_candidate_object": "FAIL",
    "test_an_unhashable_candidate_object_cannot_go_into_a_set": "FAIL",
    "test_equal_frozen_dataclasses_are_not_two_set_members": "FAIL",
    "test_a_plain_candidate_object_keeps_identity_semantics": "PASS",
    "test_classes_and_functions_hash_by_identity": "PASS",
}
_REFUSED = {
    "test_a_rewritten_hash_of_a_str_subclass_is_not_made_up",
    "test_a_rewritten_hash_cannot_pass_either_way",
    "test_an_inherited_rewritten_hash_of_a_str_subclass",
    "test_an_unhashable_str_subclass_cannot_go_into_a_set",
    "test_a_rewritten_hash_of_a_candidate_object_is_not_made_up",
    "test_an_inherited_rewritten_hash_of_a_candidate_object",
    "test_an_unhashable_candidate_object_cannot_go_into_a_set",
    "test_equal_frozen_dataclasses_are_not_two_set_members",
}


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


def test_a_rewritten_hash_is_refused_and_builtin_hashing_kept(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
    for name in _REFUSED:
        assert cases[name][1] == _REASON, (name, cases[name], output[-4000:])
