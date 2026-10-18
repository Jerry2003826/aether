"""What candidate code writes to sys.stdout/sys.stderr reaches the test, as in-process (Codex r4308195956).

The candidate process's streams went to /dev/null, so capsys (or redirect_stdout) never saw a print:
a candidate that wrongly prints passed `out == ""`, and a correct one that must print failed. Now the
candidate's sys.stdout/sys.stderr collect the writes of each request and the reply carries them, in
order; the test process writes them to its own sys.stdout/sys.stderr before the call returns or raises.
More than 1 MiB in one request is unsupported (`candidate_output_too_large`), never cut.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-output"

_APP = '''
import sys


def greet(name):
    print(f"hello {name}")


def warn():
    print("careful", file=sys.stderr)


def quiet():
    return 1


def noisy():
    print("debug")
    return 1


def failing():
    print("before")
    raise ValueError("x")


def raw():
    sys.stdout.write("no newline")


def huge():
    sys.stdout.write("z" * (2 * 1024 * 1024))
'''

_TESTS = '''
import contextlib
import io

import pytest

import app


def test_capsys_sees_a_print(capsys):
    app.greet("a")
    assert capsys.readouterr().out == "hello a\\n"


def test_a_noisy_function_cannot_pass_a_silence_check(capsys):
    assert app.noisy() == 1
    assert capsys.readouterr().out == ""


def test_stderr_is_seen(capsys):
    app.warn()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "careful\\n")


def test_output_keeps_its_place_among_the_tests_own(capsys):
    print("t1")
    app.greet("b")
    print("t2")
    assert capsys.readouterr().out == "t1\\nhello b\\nt2\\n"


def test_output_before_an_exception_is_seen(capsys):
    with pytest.raises(ValueError):
        app.failing()
    assert capsys.readouterr().out == "before\\n"


def test_redirect_stdout_sees_it():
    with contextlib.redirect_stdout(io.StringIO()) as buffer:
        app.greet("c")
    assert buffer.getvalue() == "hello c\\n"


def test_a_write_without_newline_is_seen(capsys):
    app.raw()
    assert capsys.readouterr().out == "no newline"


def test_a_quiet_function(capsys):
    assert app.quiet() == 1
    assert capsys.readouterr() == ("", "")


def test_too_much_output_is_unsupported(capsys):
    app.huge()
    assert len(capsys.readouterr().out) == 2 * 1024 * 1024
'''

_REASON = "candidate_object_unsupported:candidate_output_too_large"
_IN_PROCESS = {
    "test_capsys_sees_a_print": "PASS",
    "test_a_noisy_function_cannot_pass_a_silence_check": "FAIL",
    "test_stderr_is_seen": "PASS",
    "test_output_keeps_its_place_among_the_tests_own": "PASS",
    "test_output_before_an_exception_is_seen": "PASS",
    "test_redirect_stdout_sees_it": "PASS",
    "test_a_write_without_newline_is_seen": "PASS",
    "test_a_quiet_function": "PASS",
    "test_too_much_output_is_unsupported": "PASS",
}
_REFUSED = {"test_too_much_output_is_unsupported"}


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


def test_the_test_sees_the_candidates_output_as_in_process(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
    for name in _REFUSED:
        assert cases[name][1] == _REASON, (name, cases[name], output[-4000:])
