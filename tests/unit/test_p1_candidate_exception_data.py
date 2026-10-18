"""An exception raised by candidate code keeps its data across the isolation boundary (Codex r4307762554).

A trusted test that checks `exc.value.args`, a field the candidate's exception class set, an OSError's
errno, a StopIteration's value, an explicit cause, or the whole message must see what an in-process
pytest run sees. Before, only `str(exc)[:2000]` crossed and the builtin type was rebuilt from that one
string, so these checks diverged; a length check could even pass only because the message was cut.
An exception that cannot be rebuilt faithfully, or that is too large to cross, is refused as
unsupported (it can never pass), never cut or approximated.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-exception-data"

_APP = '''
class FieldError(KeyError):
    def __init__(self, field):
        super().__init__(field)
        self.field = field


class Loud(ValueError):
    def __str__(self):
        return "loud"


class Thing:
    x = 3


def multi():
    raise ValueError("first", 2, ("t",))


def long_message():
    raise ValueError("x" * 5000)


def field():
    raise FieldError("name")


def os_error():
    raise FileNotFoundError(2, "No such file", "missing.txt")


def unicode_error():
    return b"\\xff".decode("utf-8")


def noted():
    error = ValueError("n")
    error.add_note("hint")
    raise error


def import_error():
    raise ImportError("no module", name="mod", path="/nowhere")


def missing_attribute():
    return None.missing


def returns_five():
    return 5
    yield


def chained():
    raise ValueError("outer") from KeyError("inner")


def in_handler():
    try:
        {}["k"]
    except KeyError:
        raise ValueError("while handling")


def with_object():
    raise ValueError(Thing())


def loud():
    raise Loud("quiet")


def huge():
    raise ValueError("y" * (2 * 1024 * 1024))
'''

_TESTS = '''
import pytest

import app


def test_every_argument_of_an_error_is_kept():
    with pytest.raises(ValueError) as caught:
        app.multi()
    assert caught.value.args == ("first", 2, ("t",))


def test_a_long_message_is_whole():
    with pytest.raises(ValueError) as caught:
        app.long_message()
    assert len(str(caught.value)) == 5000


def test_a_cut_message_cannot_pass_a_length_check():
    with pytest.raises(ValueError) as caught:
        app.long_message()
    assert len(str(caught.value)) <= 2000


def test_fields_set_by_a_candidate_error_are_kept():
    with pytest.raises(KeyError) as caught:
        app.field()
    assert caught.value.field == "name"
    assert caught.value.args == ("name",)


def test_os_error_fields():
    with pytest.raises(FileNotFoundError) as caught:
        app.os_error()
    assert caught.value.errno == 2
    assert caught.value.strerror == "No such file"
    assert caught.value.filename == "missing.txt"


def test_a_unicode_decode_error_is_rebuilt():
    with pytest.raises(UnicodeDecodeError) as caught:
        app.unicode_error()
    assert caught.value.start == 0
    assert caught.value.encoding == "utf-8"


def test_notes_are_kept():
    with pytest.raises(ValueError) as caught:
        app.noted()
    assert caught.value.__notes__ == ["hint"]


def test_import_error_name_and_path():
    with pytest.raises(ImportError) as caught:
        app.import_error()
    assert caught.value.name == "mod"
    assert caught.value.path == "/nowhere"


def test_attribute_error_name():
    with pytest.raises(AttributeError) as caught:
        app.missing_attribute()
    assert caught.value.name == "missing"


def test_stop_iteration_value():
    with pytest.raises(StopIteration) as caught:
        next(app.returns_five())
    assert caught.value.value == 5


def test_an_explicit_cause_is_kept():
    with pytest.raises(ValueError) as caught:
        app.chained()
    assert isinstance(caught.value.__cause__, KeyError)
    assert caught.value.__cause__.args == ("inner",)


def test_a_hidden_cause_cannot_pass():
    with pytest.raises(ValueError) as caught:
        app.chained()
    assert caught.value.__cause__ is None


def test_the_context_is_kept():
    with pytest.raises(ValueError) as caught:
        app.in_handler()
    assert isinstance(caught.value.__context__, KeyError)


def test_a_candidate_object_in_the_arguments():
    with pytest.raises(ValueError) as caught:
        app.with_object()
    assert caught.value.args[0].x == 3


def test_an_overridden_str_is_the_candidates():
    with pytest.raises(ValueError, match="^loud$"):
        app.loud()


def test_an_error_too_large_to_cross_is_unsupported():
    with pytest.raises(ValueError):
        app.huge()
'''

# What a plain in-process pytest run gives, except the one case the protocol refuses on purpose.
_EXPECTED = {
    "test_every_argument_of_an_error_is_kept": "PASS",
    "test_a_long_message_is_whole": "PASS",
    "test_a_cut_message_cannot_pass_a_length_check": "FAIL",
    "test_fields_set_by_a_candidate_error_are_kept": "PASS",
    "test_os_error_fields": "PASS",
    "test_a_unicode_decode_error_is_rebuilt": "PASS",
    "test_notes_are_kept": "PASS",
    "test_import_error_name_and_path": "PASS",
    "test_attribute_error_name": "PASS",
    "test_stop_iteration_value": "PASS",
    "test_an_explicit_cause_is_kept": "PASS",
    "test_a_hidden_cause_cannot_pass": "FAIL",
    "test_the_context_is_kept": "PASS",
    "test_a_candidate_object_in_the_arguments": "PASS",
    "test_an_overridden_str_is_the_candidates": "PASS",
    "test_an_error_too_large_to_cross_is_unsupported": "FAIL",
}
_REFUSED = {"test_an_error_too_large_to_cross_is_unsupported": "candidate_object_unsupported:exception_too_large"}


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
    # The expectations are what plain pytest says, apart from the case refused on purpose.
    found: dict[str, str] = {}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", "test_app.py"],
        cwd=_work(tmp_path), env=_env(), capture_output=True, text=True, timeout=300, check=False,
    )
    for verdict, name in re.findall(r"^(PASSED|FAILED) test_app\.py::(\w+)", completed.stdout, re.MULTILINE):
        found[name] = "PASS" if verdict == "PASSED" else "FAIL"
    expected = {name: ("PASS" if name in _REFUSED else verdict) for name, verdict in _EXPECTED.items()}
    assert found == expected, completed.stdout[-4000:]


def test_candidate_exceptions_keep_their_data(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == _EXPECTED, output[-4000:]
    for name, detail in _REFUSED.items():
        assert cases[name][1] == detail, output[-4000:]
