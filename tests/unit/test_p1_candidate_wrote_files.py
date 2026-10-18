"""Candidate code that writes files is refused, never graded against a view that misses them (Codex r4308195953).

The candidate process runs in its private src/ directory (TMPDIR its tmp/) while the test process stays
in the restored grading workspace, so a file the candidate created, deleted or changed was invisible to
the test: a candidate that wrongly creates a forbidden file passed `assert not Path("forbidden").exists()`.
Syncing the candidate's files to the trusted side would carry candidate-written content into the test
process and the grading workspace, so it is not done (ADR-F44): the test side fingerprints the
candidate's src/ and tmp/ around every request, and any change fails the test with
candidate_object_unsupported:candidate_wrote_files, for the rest of that test module as well.
Reading files, and writing nothing, works as before.

Not candidate-written files (ADR-F44): the temporary directory, which is no longer private to the
candidate: the test process runs with the same TMPDIR (the step's tmp/), so tempfile.gettempdir(),
pytest's tmp_path and a scratch file mean the same thing to both sides, as in-process; and bytecode
caches (`__pycache__` anywhere under src/), which Python writes on import.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "nonce-for-candidate-files-shared"

_APP = '''
import tempfile
from pathlib import Path


def create(name):
    Path(name).write_text("x")


def remove(name):
    Path(name).unlink()


def read(name):
    return Path(name).read_text()


def write_in(folder):
    Path(folder, "out.txt").write_text("y")


def temp_dir():
    return tempfile.gettempdir()


def own_source():
    return Path("app.py").read_text()[:8]


def scratch():
    path = Path(tempfile.gettempdir(), "scratch.txt")
    path.write_text("s")
    return path.read_text()


def mkdir(name):
    Path(name).mkdir()


def write_in(folder):
    Path(folder, "out.txt").write_text("y")


def read(path):
    return Path(path).read_text()


def cache_note():
    Path("__pycache__").mkdir(exist_ok=True)
    Path("__pycache__", "note.txt").write_text("c")
'''

_TESTS = '''
import tempfile
from pathlib import Path

import app


def test_reading_its_own_files_still_works():
    assert app.own_source() == "\\nimport "


def test_a_temporary_file_is_shared():
    path = Path(tempfile.gettempdir(), "scratch.txt")
    assert app.scratch() == "s"
    assert path.read_text() == "s"


def test_the_temporary_directory_is_the_same():
    assert app.temp_dir() == tempfile.gettempdir()


def test_tmp_path_is_shared(tmp_path):
    app.write_in(str(tmp_path))
    assert (tmp_path / "out.txt").read_text() == "y"


def test_a_temporary_file_the_test_writes_is_read_by_the_candidate(tmp_path):
    (tmp_path / "in.txt").write_text("hi")
    assert app.read(str(tmp_path / "in.txt")) == "hi"


def test_a_bytecode_cache_write_is_not_a_written_file():
    app.cache_note()
    assert app.own_source() == "\\nimport "


def test_a_file_the_candidate_creates_is_refused():
    app.create("made.txt")
    assert Path("made.txt").read_text() == "x"


def test_a_forbidden_file_cannot_pass_unseen():
    app.create("forbidden")
    assert not Path("forbidden").exists()


def test_zz_a_later_test_cannot_miss_an_earlier_write():
    assert not Path("forbidden").exists()
'''


_TESTS_DIR = '''
from pathlib import Path

import app


def test_reading_works():
    assert app.own_source() == "\\nimport "


def test_a_directory_the_candidate_makes_cannot_pass_unseen():
    app.mkdir("made")
    assert not Path("made").exists()

'''

_REASON = "candidate_object_unsupported:candidate_wrote_files"
_IN_PROCESS = {
    "test_a_file_the_candidate_creates_is_refused": "PASS",
    "test_a_forbidden_file_cannot_pass_unseen": "FAIL",
    "test_reading_its_own_files_still_works": "PASS",
    "test_a_temporary_file_is_shared": "PASS",
    "test_the_temporary_directory_is_the_same": "PASS",
    "test_tmp_path_is_shared": "PASS",
    "test_a_temporary_file_the_test_writes_is_read_by_the_candidate": "PASS",
    "test_a_bytecode_cache_write_is_not_a_written_file": "PASS",
    "test_zz_a_later_test_cannot_miss_an_earlier_write": "FAIL",
}
_REFUSED = {
    "test_a_file_the_candidate_creates_is_refused",
    "test_a_forbidden_file_cannot_pass_unseen",
    "test_zz_a_later_test_cannot_miss_an_earlier_write",
}
_IN_PROCESS_DIR = {
    "test_a_directory_the_candidate_makes_cannot_pass_unseen": "FAIL",
    "test_reading_works": "PASS",
}
_REFUSED_DIR = {"test_a_directory_the_candidate_makes_cannot_pass_unseen"}


def _work(tmp_path: Path, tests: str = _TESTS) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text(_APP, encoding="utf-8")
    (work / "test_app.py").write_text(tests, encoding="utf-8")
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


def _in_process(work: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", "test_app.py"],
        cwd=work, env=_env(), capture_output=True, text=True, timeout=300, check=False,
    )
    for verdict, name in re.findall(r"^(PASSED|FAILED) test_app\.py::(\w+)", completed.stdout, re.MULTILINE):
        found[name] = "PASS" if verdict == "PASSED" else "FAIL"
    return found


@pytest.mark.parametrize(("tests", "expected"), [(_TESTS, _IN_PROCESS), (_TESTS_DIR, _IN_PROCESS_DIR)], ids=["files", "directory"])
def test_the_in_process_run_gives_the_expected_verdicts(tmp_path: Path, tests: str, expected: dict[str, str]) -> None:
    assert _in_process(_work(tmp_path, tests)) == expected


@pytest.mark.parametrize(
    ("tests", "in_process", "refused"), [(_TESTS, _IN_PROCESS, _REFUSED), (_TESTS_DIR, _IN_PROCESS_DIR, _REFUSED_DIR)],
    ids=["files", "directory"],
)
def test_a_candidate_that_writes_files_is_refused_by_name(
    tmp_path: Path, tests: str, in_process: dict[str, str], refused: set[str]
) -> None:
    cases, output = _grade(_work(tmp_path, tests))
    expected = {name: ("FAIL" if name in refused else verdict) for name, verdict in in_process.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
    assert {name for name, (_verdict, detail) in cases.items() if detail == _REASON} == refused, output[-4000:]
