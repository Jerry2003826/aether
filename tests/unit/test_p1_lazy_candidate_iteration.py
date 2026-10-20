"""Iterating a candidate object takes one item at a time, as in-process (Codex r4308195951).

`_Remote.__iter__` asked the candidate for `list(target)` and iterated the copy: an iterator was
exhausted at once, before the test changed candidate state between two items, so a state-dependent
iterator returned the old (expected) item through the proxy and a different one in-process, and an
infinite generator never answered. `reversed()` was copied the same way. Now iter() and reversed()
return the candidate's own iterator and every item is one `next` in the candidate process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-lazy-candidate-iteration"

_APP = '''
class Steps:
    def __init__(self):
        self.step = 1

    def __iter__(self):
        n = 0
        while n < 6:
            n += self.step
            yield n


steps = Steps()


def naturals():
    n = 0
    while True:
        n += 1
        yield n


def numbers():
    return iter([1, 2, 3])


class Seq:
    def __init__(self):
        self.items = [1, 2, 3]

    def __reversed__(self):
        for index in range(len(self.items) - 1, -1, -1):
            yield self.items[index]


seq = Seq()
'''

_TESTS = '''
import app


def test_a_state_change_between_items_is_seen():
    app.steps.step = 1
    items = iter(app.steps)
    assert next(items) == 1
    app.steps.step = 2
    assert next(items) == 3


def test_the_old_item_cannot_pass_after_a_state_change():
    app.steps.step = 1
    items = iter(app.steps)
    assert next(items) == 1
    app.steps.step = 2
    assert next(items) == 2


def test_an_infinite_generator_gives_items_one_at_a_time():
    items = iter(app.naturals())
    assert [next(items) for _ in range(3)] == [1, 2, 3]


def test_a_loop_over_an_infinite_generator_can_stop():
    seen = []
    for value in app.naturals():
        seen.append(value)
        if value == 3:
            break
    assert seen == [1, 2, 3]


def test_a_finite_iterator_is_listed_and_exhausted():
    items = app.numbers()
    assert list(items) == [1, 2, 3]
    assert next(items, None) is None


def test_reversed_is_lazy():
    app.seq.items = [1, 2, 3]
    backwards = reversed(app.seq)
    assert next(backwards) == 3
    app.seq.items[0] = 9
    assert list(backwards) == [2, 9]


def test_reversed_cannot_pass_with_a_copy():
    app.seq.items = [1, 2, 3]
    backwards = reversed(app.seq)
    assert next(backwards) == 3
    app.seq.items[0] = 9
    assert list(backwards) == [2, 1]
'''

_IN_PROCESS = {
    "test_a_state_change_between_items_is_seen": "PASS",
    "test_the_old_item_cannot_pass_after_a_state_change": "FAIL",
    "test_an_infinite_generator_gives_items_one_at_a_time": "PASS",
    "test_a_loop_over_an_infinite_generator_can_stop": "PASS",
    "test_a_finite_iterator_is_listed_and_exhausted": "PASS",
    "test_reversed_is_lazy": "PASS",
    "test_reversed_cannot_pass_with_a_copy": "FAIL",
}
_REFUSED: set[str] = set()


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


def test_candidate_iteration_is_lazy_as_in_process(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
