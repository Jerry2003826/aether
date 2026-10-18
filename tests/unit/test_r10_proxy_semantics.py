"""Candidate objects behave in a trusted test as they would in-process (Codex 16:04 :512, 16:41 :627).

The test and the candidate run in separate processes. Two things must still hold:
- a builtin list, dict, set or bytearray passed across is the same object to both sides (in-place
  changes, kept references, identity);
- an operation the proxy does not carry must never surface as Python's own TypeError, which
  `pytest.raises(TypeError)` or `assertRaises(TypeError)` would take for the candidate's behaviour.
Each test below has the verdict a plain in-process pytest run gives it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "n0nce-7c2d4e9a1b3f5a60"


def _grade(root: Path) -> tuple[int, dict[str, str], str]:
    secret = root.parent / (root.name + ".nonce")
    secret.write_text(_NONCE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env["AETHER_PROTOCOL_NONCE_FILE"] = str(secret)
    env["AETHER_PROTOCOL_SHM_DIR"] = "/dev/shm"
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)], cwd=root, env=env, capture_output=True, text=True, timeout=180, check=False
    )
    output = completed.stdout + completed.stderr
    signed = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{") and _NONCE in line]
    assert len(signed) == 1, output
    return completed.returncode, {case["name"]: case["verdict"] for case in signed[0]["cases"]}, output


def _suite(tmp_path: Path, app: str, tests: str) -> tuple[int, dict[str, str], str]:
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text(app, encoding="utf-8")
    (work / "test_app.py").write_text(tests, encoding="utf-8")
    return _grade(work)


_REFERENCE_APP = '''
def add(items, value):
    items.append(value)


def total(items):
    # Wrong: empties the caller's list.
    result = sum(items)
    items.clear()
    return result


def fill(mapping, members, buffer):
    mapping["k"] = 1
    members.add(1)
    buffer[:] = buffer.upper()


def push_inner(outer):
    outer[0].append(2)


def same(items):
    return items


class Bag:
    def __init__(self, items):
        self.items = items

    def add(self, value):
        self.items.append(value)

    def count(self):
        return len(self.items)


class Snapshot:
    # Wrong: keeps the caller's list instead of a copy.
    def __init__(self, items):
        self.items = items

    def size(self):
        return len(self.items)
'''

_REFERENCE_TESTS = '''
import app


def test_in_place_append_reaches_the_caller():
    items = []
    app.add(items, 1)
    assert items == [1]


def test_clearing_the_callers_list_is_caught():
    items = [1, 2]
    assert app.total(items) == 3
    assert items == [1, 2]


def test_dict_set_and_bytearray_change_in_place():
    mapping, members, buffer = {}, set(), bytearray(b"ab")
    app.fill(mapping, members, buffer)
    assert mapping == {"k": 1}
    assert members == {1}
    assert buffer == bytearray(b"AB")


def test_nested_lists_keep_their_identity():
    inner = [1]
    outer = [inner]
    app.push_inner(outer)
    assert inner == [1, 2]
    assert outer[0] is inner


def test_a_kept_reference_sees_later_changes_both_ways():
    items = []
    bag = app.Bag(items)
    bag.add(1)
    assert items == [1]
    items.append(2)
    assert bag.count() == 2
    got = bag.items
    assert got is items


def test_the_same_list_comes_back():
    items = []
    assert app.same(items) is items


def test_a_stored_alias_is_caught():
    items = [1]
    snapshot = app.Snapshot(items)
    items.append(2)
    assert snapshot.size() == 1
'''


def test_builtin_containers_are_shared_by_reference_with_candidate_code(tmp_path: Path) -> None:
    """:512: in-place changes reach the caller (so items.clear() in a wrong implementation fails and a
    correct in-place change passes), references kept by the candidate stay live in both directions, and
    identity holds."""
    code, verdicts, output = _suite(tmp_path, _REFERENCE_APP, _REFERENCE_TESTS)
    assert verdicts == {
        "test_app.test_in_place_append_reaches_the_caller": "PASS",
        "test_app.test_clearing_the_callers_list_is_caught": "FAIL",
        "test_app.test_dict_set_and_bytearray_change_in_place": "PASS",
        "test_app.test_nested_lists_keep_their_identity": "PASS",
        "test_app.test_a_kept_reference_sees_later_changes_both_ways": "PASS",
        "test_app.test_the_same_list_comes_back": "PASS",
        "test_app.test_a_stored_alias_is_caught": "FAIL",
    }, output
    assert code == 1, output


_OPERATOR_APP = '''
class Money:
    def __init__(self, amount):
        self.amount = amount

    def __add__(self, other):
        # Wrong: accepts anything, a str included.
        return Money(self.amount + (other.amount if isinstance(other, Money) else len(str(other))))

    __radd__ = __add__

    def __neg__(self):
        return Money(-self.amount)

    def __int__(self):
        return self.amount

    def __index__(self):
        return self.amount

    def __format__(self, spec):
        return format(self.amount, spec)


class Store:
    def __init__(self):
        self.data = {}

    def __setitem__(self, key, value):
        self.data[key] = value

    def __delitem__(self, key):
        del self.data[key]

    def __len__(self):
        return len(self.data)


class Resource:
    def __init__(self):
        self.state = "new"

    def __enter__(self):
        self.state = "open"
        return "opened"

    def __exit__(self, *exc):
        self.state = "closed"
        return False


def counter():
    return iter([1, 2])
'''

_OPERATOR_TESTS = '''
import unittest

import pytest

import app


def test_add_and_reflected_add_are_the_candidates():
    assert (app.Money(1) + 2).amount == 2
    assert (2 + app.Money(1)).amount == 2
    assert (app.Money(1) + app.Money(2)).amount == 3


def test_a_type_error_the_candidate_never_raises_is_not_faked():
    with pytest.raises(TypeError):
        app.Money(1) + "x"


class NegationTests(unittest.TestCase):
    def test_negation_type_error_is_not_faked(self):
        with self.assertRaises(TypeError):
            -app.Money(1)


def test_next_runs_in_the_candidate():
    numbers = app.counter()
    assert next(numbers) == 1
    assert next(numbers) == 2
    with pytest.raises(StopIteration):
        next(numbers)


def test_conversions_and_format_are_the_candidates():
    assert int(app.Money(3)) == 3
    assert [10, 20, 30, 40][app.Money(3)] == 40
    assert f"{app.Money(3):>4}" == "   3"


def test_del_item_runs_in_the_candidate():
    store = app.Store()
    store["k"] = 1
    del store["k"]
    assert len(store) == 0


def test_with_runs_the_candidates_context_manager():
    resource = app.Resource()
    with resource as value:
        assert value == "opened"
        assert resource.state == "open"
    assert resource.state == "closed"


def test_an_operation_the_proxy_cannot_carry_is_not_a_type_error():
    with pytest.raises(TypeError):
        app.Money(1) @ app.Money(2)
'''


def test_operations_on_candidate_objects_are_the_candidates_or_fail_the_test(tmp_path: Path) -> None:
    """:627: operators, next(), conversions, format, del and with run in the candidate process. An
    operation the proxy does not carry fails the test as unsupported; it is never a TypeError that
    pytest.raises or assertRaises would accept."""
    code, verdicts, output = _suite(tmp_path, _OPERATOR_APP, _OPERATOR_TESTS)
    assert verdicts == {
        "test_app.test_add_and_reflected_add_are_the_candidates": "PASS",
        "test_app.test_a_type_error_the_candidate_never_raises_is_not_faked": "FAIL",
        "test_app.NegationTests.test_negation_type_error_is_not_faked": "FAIL",
        "test_app.test_next_runs_in_the_candidate": "PASS",
        "test_app.test_conversions_and_format_are_the_candidates": "PASS",
        "test_app.test_del_item_runs_in_the_candidate": "PASS",
        "test_app.test_with_runs_the_candidates_context_manager": "PASS",
        # Money has no __matmul__: in-process this raises the candidate's TypeError, so it passes.
        "test_app.test_an_operation_the_proxy_cannot_carry_is_not_a_type_error": "PASS",
    }, output
    assert code == 1, output
