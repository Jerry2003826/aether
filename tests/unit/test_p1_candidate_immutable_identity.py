"""The same immutable container returned by candidate code is the same object in the test (Codex r4309057203).

Tuples and frozensets cross by value, and every occurrence was encoded and rebuilt on its own, so
`shared = (1, 2); return shared, shared` gave the test two objects: `assert result[0] is not result[1]`
passed, though it fails in-process. Now the candidate process gives each tuple or frozenset it sends a
reference id (and keeps it alive, so the id is never reused); the first occurrence carries the value,
every later one, in the same reply or a later one, only the id, and the test process keeps the object
it rebuilt for that id. Equal containers that are distinct objects stay distinct.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-immutable-identity"

_APP = '''
SHARED = tuple([1, 2])


def pair():
    shared = tuple([1, 2])
    return shared, shared


def same():
    return SHARED


def frozen():
    shared = frozenset([1])
    return [shared, shared]


def nested():
    shared = tuple([1])
    return {"a": shared, "b": (shared,)}


def distinct():
    return tuple([1, 2]), tuple([1, 2])
'''

_TESTS = '''
import app


def test_the_same_tuple_twice_is_one_object():
    result = app.pair()
    assert result[0] is result[1]


def test_the_same_tuple_cannot_pass_as_two():
    result = app.pair()
    assert result[0] is not result[1]


def test_the_same_tuple_from_two_calls_is_one_object():
    assert app.same() is app.same()


def test_the_same_frozenset_twice_is_one_object():
    result = app.frozen()
    assert result[0] is result[1]


def test_identity_holds_at_any_depth():
    result = app.nested()
    assert result["a"] is result["b"][0]


def test_equal_but_distinct_tuples_stay_distinct():
    first, second = app.distinct()
    assert first == second and first is not second
'''

_IN_PROCESS = {
    "test_the_same_tuple_twice_is_one_object": "PASS",
    "test_the_same_tuple_cannot_pass_as_two": "FAIL",
    "test_the_same_tuple_from_two_calls_is_one_object": "PASS",
    "test_the_same_frozenset_twice_is_one_object": "PASS",
    "test_identity_holds_at_any_depth": "PASS",
    "test_equal_but_distinct_tuples_stay_distinct": "PASS",
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
def test_immutable_identity_is_kept_as_in_process(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == _IN_PROCESS, output[-4000:]
