"""A candidate's instance of a builtin-type subclass keeps its class in a trusted test (Codex :1504).

`class Result(list)`, a namedtuple, an IntEnum, a str subclass, `collections.Counter` and the like are
not plain data: their methods, fields and overridden hooks (`__len__`, `__missing__`, ...) are part of
what the test checks. Each test below has the verdict a plain in-process pytest run gives it, except
the two that guard ADR-F44: a candidate class that overrides comparison still cannot make
`assert result == expected` pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-proxy-subclass-tests"


def _grade(root: Path) -> tuple[int, dict[str, tuple[str, str]], str]:
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
    cases = {case["name"]: (case["verdict"], case["detail"]) for case in signed[0]["cases"]}
    return completed.returncode, cases, output


_APP = '''
import collections
import enum


class Result(list):
    def total(self):
        return sum(self)


def make_result():
    return Result([1, 2, 3])


Point = collections.namedtuple("Point", "x y")


def point():
    return Point(1, 2)


def points():
    return [Point(1, 2), Point(3, 4)]


def norm(p):
    return p.x + p.y


class Tag(str):
    def shout(self):
        return self.upper()


def tag():
    return Tag("a")


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


def level():
    return Level.HIGH


class Window(list):
    def __len__(self):
        return 3


def window():
    return Window([1])


class Counts(dict):
    def __missing__(self, key):
        return 0


def counts():
    return Counts(a=1)


def word_counts():
    return collections.Counter("aab")


def defaults():
    made = collections.defaultdict(list)
    made["k"].append(1)
    return made


def ordered():
    return collections.OrderedDict([("b", 1), ("a", 2)])


class Always(str):
    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


def always():
    return Always("wrong")


class Sticky(list):
    def __eq__(self, other):
        return True


def sticky():
    return Sticky()
'''

_TESTS = '''
import collections

import app


def test_a_list_subclass_keeps_its_methods_and_compares_as_its_items():
    result = app.make_result()
    assert result.total() == 6
    assert result == [1, 2, 3]
    result.append(4)
    assert result == [1, 2, 3, 4]
    assert result.total() == 10
    assert len(result) == 4


def test_a_namedtuple_keeps_its_fields_and_hashes_as_its_tuple():
    p = app.point()
    assert p.x == 1 and p.y == 2
    assert p == (1, 2)
    assert p._asdict() == {"x": 1, "y": 2}
    assert {p: "here"}[(1, 2)] == "here"
    assert p in {(1, 2)}
    assert app.norm(p) == 3


def test_namedtuples_inside_a_shared_list():
    found = app.points()
    assert found == [(1, 2), (3, 4)]
    assert [p.y for p in found] == [2, 4]


def test_a_str_subclass_keeps_its_methods():
    t = app.tag()
    assert t.shout() == "A"
    assert t == "a"
    assert t + "b" == "ab"
    assert str(t) == "a"


def test_an_int_enum_keeps_its_name_order_and_identity():
    value = app.level()
    assert value.name == "HIGH"
    assert value == 2
    assert value > app.Level.LOW
    assert value is app.Level.HIGH


def test_an_overridden_len_is_the_candidates():
    assert len(app.window()) == 3


def test_a_dict_subclass_missing_hook_runs():
    found = app.counts()
    assert found["zzz"] == 0
    assert found == {"a": 1}


def test_stdlib_counter_defaultdict_and_ordereddict():
    words = app.word_counts()
    assert words["a"] == 2 and words["zzz"] == 0
    assert words == {"a": 2, "b": 1}
    assert words.most_common(1) == [("a", 2)]
    made = app.defaults()
    assert made["new"] == []
    assert made == {"k": [1], "new": []}
    kept = app.ordered()
    assert list(kept) == ["b", "a"]
    assert kept == {"a": 2, "b": 1}
    assert kept != collections.OrderedDict([("a", 2), ("b", 1)])


def test_an_always_equal_str_subclass_cannot_pass():
    assert app.always() == "expected"


def test_an_always_equal_list_subclass_cannot_pass():
    assert app.sticky() == [1]
'''


def test_builtin_subclass_instances_keep_their_class(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text(_APP, encoding="utf-8")
    (work / "test_app.py").write_text(_TESTS, encoding="utf-8")
    code, cases, output = _grade(work)
    verdicts = {name: verdict for name, (verdict, _detail) in cases.items()}
    assert verdicts == {
        "test_app.test_a_list_subclass_keeps_its_methods_and_compares_as_its_items": "PASS",
        "test_app.test_a_namedtuple_keeps_its_fields_and_hashes_as_its_tuple": "PASS",
        "test_app.test_namedtuples_inside_a_shared_list": "PASS",
        "test_app.test_a_str_subclass_keeps_its_methods": "PASS",
        "test_app.test_an_int_enum_keeps_its_name_order_and_identity": "PASS",
        "test_app.test_an_overridden_len_is_the_candidates": "PASS",
        "test_app.test_a_dict_subclass_missing_hook_runs": "PASS",
        "test_app.test_stdlib_counter_defaultdict_and_ordereddict": "PASS",
        "test_app.test_an_always_equal_str_subclass_cannot_pass": "FAIL",
        "test_app.test_an_always_equal_list_subclass_cannot_pass": "FAIL",
    }, output
    # A class that overrides comparison is refused, not compared by its data behind the test's back.
    for name in ("test_an_always_equal_str_subclass_cannot_pass", "test_an_always_equal_list_subclass_cannot_pass"):
        assert cases["test_app." + name][1] == "candidate_object_unsupported:compare_candidate_object", output
    assert code == 1, output
