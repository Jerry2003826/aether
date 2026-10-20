"""Candidate code that writes files is refused, never graded against a view that misses them (Codex r4308195953).

The candidate process runs in its private src/ directory (TMPDIR its tmp/) while the test process stays
in the restored grading workspace, so a file the candidate created, deleted or changed was invisible to
the test: a candidate that wrongly creates a forbidden file passed `assert not Path("forbidden").exists()`.
Syncing the candidate's files to the trusted side would carry candidate-written content into the test
process and the grading workspace, so it is not done (ADR-F44): the test side fingerprints the
candidate's src/ and tmp/ around every request, and any change fails the test with
candidate_object_unsupported:candidate_wrote_files, for the rest of that test module as well.
Reading files, and writing nothing, works as before.

Not candidate-written files (ADR-F44): the candidate's own temporary directory (its tmp/, its TMPDIR),
and bytecode caches (`__pycache__` anywhere under src/), which Python writes on import.

The two temporary directories are never shared (code review, Critical): the test process has its own
private TMPDIR, so tempfile.gettempdir() and pytest's tmp_path are its own, and the candidate process can
neither read nor write it (its Landlock domain holds only its src/ and tmp/). A candidate that looks
for a file under the test's tmp_path, or is handed its path in disguise, cannot rewrite it. A path inside
the test's temporary directory passed to candidate code (as str, bytes or a path object, at any depth)
is refused as candidate_object_unsupported:test_tmp_path_passed_to_candidate, never silently a path the
candidate cannot reach.

A test that looks for a candidate's temporary file in its own temporary directory cannot see it, so
`assert not Path(gettempdir(), name).exists()` would pass where it fails in-process. A test that passes
while the candidate's tmp/ still holds a file the candidate wrote during that test is therefore refused
as candidate_object_unsupported:candidate_left_tmp_files (code review); a candidate that removes its
temporary files is unaffected. What remains different from in-process is named in _DIFFERS below.
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


def scratch_cleaned():
    path = Path(tempfile.gettempdir(), "cleaned.txt")
    path.write_text("c")
    text = path.read_text()
    path.unlink()
    return text


def mkdir(name):
    Path(name).mkdir()


def write_in(folder):
    Path(folder, "out.txt").write_text("y")


def read(path):
    return Path(path).read_text()


def cache_note():
    Path("__pycache__").mkdir(exist_ok=True)
    Path("__pycache__", "note.txt").write_text("c")


def echo(value):
    return value


def tamper_nearby():
    # Every victim.txt under this process's temporary directory and its parent.
    import os

    done = []
    seen = set()
    for top in (tempfile.gettempdir(), os.path.dirname(tempfile.gettempdir())):
        for folder, _dirs, files in os.walk(top):
            if folder in seen or "victim.txt" not in files:
                continue
            seen.add(folder)
            try:
                Path(folder, "victim.txt").write_text("candidate")
                done.append("wrote")
            except OSError as exc:
                done.append(type(exc).__name__)
    return done


def tamper_disguised(reversed_path):
    try:
        Path(reversed_path[::-1]).write_text("candidate")
        return "wrote"
    except OSError as exc:
        return type(exc).__name__
'''

_TESTS = '''
import tempfile
from pathlib import Path

import app


def test_reading_its_own_files_still_works():
    assert app.own_source() == "\\nimport "


def test_a_scratch_file_left_in_the_candidates_tmp_is_refused():
    assert app.scratch() == "s"


def test_a_scratch_file_the_candidate_removes_is_not_refused():
    assert app.scratch_cleaned() == "c"


def test_the_candidate_cannot_rewrite_a_file_under_tmp_path_it_was_not_given(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("trusted")
    app.tamper_nearby()
    assert victim.read_text() == "trusted"


def test_the_candidate_cannot_rewrite_a_file_under_tmp_path_by_a_disguised_path(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("trusted")
    app.tamper_disguised(str(victim)[::-1])
    assert victim.read_text() == "trusted"


def test_passing_tmp_path_to_the_candidate_is_refused(tmp_path):
    app.write_in(str(tmp_path))
    assert (tmp_path / "out.txt").read_text() == "y"


def test_passing_a_file_under_tmp_path_is_refused(tmp_path):
    (tmp_path / "in.txt").write_text("hi")
    assert app.read(str(tmp_path / "in.txt")) == "hi"


def test_passing_a_path_object_under_tmp_path_is_refused(tmp_path):
    (tmp_path / "in.txt").write_text("hi")
    assert app.read(tmp_path / "in.txt") == "hi"


def test_passing_the_temporary_directory_nested_is_refused():
    assert app.echo({"dirs": [tempfile.gettempdir().encode()]}) == {"dirs": [tempfile.gettempdir().encode()]}


def test_other_paths_still_cross():
    assert app.echo("/etc/hostname") == "/etc/hostname"


def test_the_temporary_directories_are_not_shared():
    assert app.temp_dir() == tempfile.gettempdir()


def test_a_scratch_file_the_candidate_wrote_is_not_in_the_tests_tmp():
    app.scratch()
    assert not Path(tempfile.gettempdir(), "scratch.txt").exists()


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
_TMP_PATH = "candidate_object_unsupported:test_tmp_path_passed_to_candidate"
_LEFT_TMP = "candidate_object_unsupported:candidate_left_tmp_files"
_IN_PROCESS = {
    "test_a_file_the_candidate_creates_is_refused": "PASS",
    "test_a_forbidden_file_cannot_pass_unseen": "FAIL",
    "test_reading_its_own_files_still_works": "PASS",
    "test_a_scratch_file_left_in_the_candidates_tmp_is_refused": "PASS",
    "test_a_scratch_file_the_candidate_removes_is_not_refused": "PASS",
    "test_the_candidate_cannot_rewrite_a_file_under_tmp_path_it_was_not_given": "FAIL",
    "test_the_candidate_cannot_rewrite_a_file_under_tmp_path_by_a_disguised_path": "FAIL",
    "test_passing_tmp_path_to_the_candidate_is_refused": "PASS",
    "test_passing_a_file_under_tmp_path_is_refused": "PASS",
    "test_passing_a_path_object_under_tmp_path_is_refused": "PASS",
    "test_passing_the_temporary_directory_nested_is_refused": "PASS",
    "test_other_paths_still_cross": "PASS",
    "test_the_temporary_directories_are_not_shared": "PASS",
    "test_a_scratch_file_the_candidate_wrote_is_not_in_the_tests_tmp": "FAIL",
    "test_a_bytecode_cache_write_is_not_a_written_file": "PASS",
    "test_zz_a_later_test_cannot_miss_an_earlier_write": "FAIL",
}
_REFUSED = {
    "test_a_file_the_candidate_creates_is_refused": _REASON,
    "test_a_forbidden_file_cannot_pass_unseen": _REASON,
    "test_zz_a_later_test_cannot_miss_an_earlier_write": _REASON,
    "test_passing_tmp_path_to_the_candidate_is_refused": _TMP_PATH,
    "test_passing_a_file_under_tmp_path_is_refused": _TMP_PATH,
    "test_passing_a_path_object_under_tmp_path_is_refused": _TMP_PATH,
    "test_passing_the_temporary_directory_nested_is_refused": _TMP_PATH,
    "test_a_scratch_file_left_in_the_candidates_tmp_is_refused": _LEFT_TMP,
    "test_a_scratch_file_the_candidate_wrote_is_not_in_the_tests_tmp": _LEFT_TMP,
}
# Isolation that in-process lacks: the candidate cannot reach the test's temporary directory at all.
_PROTECTED = {
    "test_the_candidate_cannot_rewrite_a_file_under_tmp_path_it_was_not_given",
    "test_the_candidate_cannot_rewrite_a_file_under_tmp_path_by_a_disguised_path",
}
# Not detectable, recorded in ADR-F44: a test that compares its own temporary directory with the
# candidate's sees two different directories (in-process PASS, here FAIL; never a false pass).
_DIFFERS = {
    "test_the_temporary_directories_are_not_shared": ("FAIL", "AssertionError"),
}
_IN_PROCESS_DIR = {
    "test_a_directory_the_candidate_makes_cannot_pass_unseen": "FAIL",
    "test_reading_works": "PASS",
}
_REFUSED_DIR = {"test_a_directory_the_candidate_makes_cannot_pass_unseen": _REASON}


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
    # One temporary directory for the test and the candidate code, beside the work directory, as
    # in-process; tamper_nearby() walks only its parent.
    env = _env()
    shared = work.parent / "tmp"
    shared.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = str(shared)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", "test_app.py"],
        cwd=work, env=env, capture_output=True, text=True, timeout=300, check=False,
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
    tmp_path: Path, tests: str, in_process: dict[str, str], refused: dict[str, str]
) -> None:
    cases, output = _grade(_work(tmp_path, tests))
    expected: dict[str, tuple[str, str]] = {}
    for name, verdict in in_process.items():
        if name in refused:
            expected[name] = ("FAIL", refused[name])
        elif name in _PROTECTED:
            expected[name] = ("PASS", "")
        elif name in _DIFFERS:
            expected[name] = _DIFFERS[name]
        else:
            expected[name] = (verdict, cases.get(name, ("", ""))[1] if verdict == "FAIL" else "")
    assert cases == expected, output[-4000:]
