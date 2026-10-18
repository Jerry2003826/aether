"""Candidate Enum members work as dict keys and set members; other stdlib hashes are refused (parent steering on r4308195950).

The c00f6b2 check refused hash() unless the class providing __hash__ came from the builtins module. That
also refused every Enum member (enum.Enum defines __hash__), so `{app.Color.RED: 1}` failed as
unsupported although an Enum member, a singleton compared by identity, has exactly the set and dict
semantics of the proxy's identity hash. And it trusted `__module__`, which a class can set to
"builtins": a class with its own __hash__ could still get an invented identity hash.

Now identity hashing is used only when the __hash__ that hash() would run IS object.__hash__ (also
when a class names it explicitly), or IS enum.Enum.__hash__ as it was before any candidate code ran,
with __eq__ left to object's identity. Everything else is unsupported (hash_of_candidate_object): an
Enum that rewrites __hash__, a class claiming builtins, Enum.__hash__ patched by candidate code, and
stdlib classes that hash their data (date, Decimal, Fraction), whose equal values would wrongly be two
set members. An IntEnum member hashes as its int data, as before.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-stdlib-hash"

_APP = '''
import datetime
import decimal
import enum
import fractions


class Color(enum.Enum):
    RED = 1
    GREEN = 2


class Perm(enum.Flag):
    R = 1
    W = 2


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class OwnHash(enum.Enum):
    A = 1

    def __hash__(self):
        return 5


class Sneaky:
    __module__ = "builtins"

    def __hash__(self):
        return 7


class Explicit:
    __hash__ = object.__hash__


SHARED = Explicit()


def sneaky():
    return Sneaky()


def shared():
    return SHARED


def explicit():
    return Explicit()


def day():
    return datetime.date(2026, 1, 1)


def amount():
    return decimal.Decimal("1.5")


def ratio():
    return fractions.Fraction(1, 2)


def patch_enum():
    enum.Enum.__hash__ = lambda self: 123
'''

_TESTS = '''
import app


def test_an_enum_member_is_a_dict_key():
    table = {app.Color.RED: "r"}
    assert table[app.Color.RED] == "r"


def test_enum_members_in_a_set():
    assert len({app.Color.RED, app.Color.RED, app.Color.GREEN}) == 2


def test_a_flag_member_in_a_set():
    assert app.Perm.R in {app.Perm.R}


def test_an_int_enum_member_hashes_as_its_int():
    assert {app.Level.HIGH: 1}[2] == 1


def test_an_enum_with_its_own_hash_cannot_pass():
    assert hash(app.OwnHash.A) != 5


def test_a_class_claiming_builtins_cannot_invent_a_hash():
    assert hash(app.sneaky()) != 7


def test_an_explicit_object_hash_is_identity():
    same = app.shared()
    assert len({same, same}) == 1
    assert len({app.explicit(), app.explicit()}) == 2


def test_equal_dates_are_one_set_member():
    assert len({app.day(), app.day()}) == 1


def test_equal_dates_cannot_be_two_set_members():
    assert len({app.day(), app.day()}) == 2


def test_a_decimal_hashes_its_value():
    assert len({app.amount(), app.amount()}) == 1


def test_a_fraction_hashes_its_value():
    assert len({app.ratio(), app.ratio()}) == 1


def test_zz_an_enum_hash_patched_by_candidate_code_cannot_pass():
    app.patch_enum()
    assert hash(app.Color.RED) != 123
'''

_REASON = "candidate_object_unsupported:hash_of_candidate_object"
_IN_PROCESS = {
    "test_an_enum_member_is_a_dict_key": "PASS",
    "test_enum_members_in_a_set": "PASS",
    "test_a_flag_member_in_a_set": "PASS",
    "test_an_int_enum_member_hashes_as_its_int": "PASS",
    "test_an_enum_with_its_own_hash_cannot_pass": "FAIL",
    "test_a_class_claiming_builtins_cannot_invent_a_hash": "FAIL",
    "test_an_explicit_object_hash_is_identity": "PASS",
    "test_equal_dates_are_one_set_member": "PASS",
    "test_equal_dates_cannot_be_two_set_members": "FAIL",
    "test_a_decimal_hashes_its_value": "PASS",
    "test_a_fraction_hashes_its_value": "PASS",
    "test_zz_an_enum_hash_patched_by_candidate_code_cannot_pass": "FAIL",
}
_REFUSED = {
    "test_an_enum_with_its_own_hash_cannot_pass",
    "test_a_class_claiming_builtins_cannot_invent_a_hash",
    "test_equal_dates_are_one_set_member",
    "test_equal_dates_cannot_be_two_set_members",
    "test_a_decimal_hashes_its_value",
    "test_a_fraction_hashes_its_value",
    "test_zz_an_enum_hash_patched_by_candidate_code_cannot_pass",
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


def test_enum_members_hash_and_other_stdlib_hashes_are_refused(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
    for name in _REFUSED:
        assert cases[name][1] == _REASON, (name, cases[name], output[-4000:])
