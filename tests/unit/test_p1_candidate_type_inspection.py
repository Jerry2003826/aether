"""type() and .__class__ of a candidate object are refused by name, never answered by the proxy (Codex r4309057196).

A candidate object is a handle in the test process, so `type(app.make())` was the proxy class and
`app.make().__class__` the proxy's: `assert type(app.make()) is not object` passed when make() returns
`object()`, though it fails in-process. The proxy cannot be given the candidate's runtime type without
the candidate's class in the test process, so the trusted sources (test modules, trusted helpers,
conftests) are compiled with `type(x)` (one argument) and `x.__class__` (read) going through guards:
for a candidate object (handle, builtin-subclass handle or module) they fail the test as
candidate_object_unsupported:type_of_candidate_object / class_of_candidate_object; for anything else
they are exactly the builtin. Data candidate code returns by value (an int, a tuple) keeps its type.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-type-inspection"

_APP = '''
class Thing:
    pass


class Result(list):
    pass


def make():
    return object()


def thing():
    return Thing()


def result():
    return Result([1])


def number():
    return 3
'''

_TESTS = '''
import app


def test_the_type_of_a_plain_object_is_object():
    assert type(app.make()) is object


def test_a_plain_object_cannot_pass_as_not_an_object():
    assert type(app.make()) is not object


def test_its_class_cannot_pass_as_not_object():
    assert app.make().__class__ is not object


def test_the_type_of_a_candidate_instance_is_its_class():
    assert type(app.thing()) is app.Thing


def test_the_type_of_a_builtin_subclass_instance():
    assert type(app.result()) is not list


def test_the_type_of_the_candidate_module():
    assert type(app).__name__ == "module"


def test_data_returned_by_value_keeps_its_type():
    assert type(app.number()) is int and app.number().__class__ is int


def test_type_and_class_of_the_tests_own_values():
    assert type(1) is int and (1).__class__ is int and type([]) is list


def test_three_argument_type_still_builds_a_class():
    kind = type("Kind", (), {})
    assert kind.__name__ == "Kind" and type(kind()) is kind


def test_isinstance_is_unaffected():
    assert isinstance(app.result(), list)
'''

_TYPE = "candidate_object_unsupported:type_of_candidate_object"
_CLASS = "candidate_object_unsupported:class_of_candidate_object"
_IN_PROCESS = {
    "test_the_type_of_a_plain_object_is_object": "PASS",
    "test_a_plain_object_cannot_pass_as_not_an_object": "FAIL",
    "test_its_class_cannot_pass_as_not_object": "FAIL",
    "test_the_type_of_a_candidate_instance_is_its_class": "PASS",
    "test_the_type_of_a_builtin_subclass_instance": "PASS",
    "test_the_type_of_the_candidate_module": "PASS",
    "test_data_returned_by_value_keeps_its_type": "PASS",
    "test_type_and_class_of_the_tests_own_values": "PASS",
    "test_three_argument_type_still_builds_a_class": "PASS",
    "test_isinstance_is_unaffected": "PASS",
}
_REFUSED = {
    "test_the_type_of_a_plain_object_is_object": _TYPE,
    "test_a_plain_object_cannot_pass_as_not_an_object": _TYPE,
    "test_its_class_cannot_pass_as_not_object": _CLASS,
    "test_the_type_of_a_candidate_instance_is_its_class": _TYPE,
    "test_the_type_of_a_builtin_subclass_instance": _TYPE,
    "test_the_type_of_the_candidate_module": _TYPE,
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
def test_type_inspection_of_candidate_objects_is_refused_by_name(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
    for name, reason in _REFUSED.items():
        assert cases[name][1] == reason, (name, cases[name], output[-4000:])
