"""The test and candidate code see the same files, as in-process (Codex r4308195953).

The candidate process ran in its private src/ directory (TMPDIR its tmp/) while the test process stayed
in the restored grading workspace with its own TMPDIR, so a file the candidate created, deleted or
changed was invisible to the test: a candidate that wrongly creates a forbidden file passed
`assert not Path("forbidden").exists()`, and a file the test wrote was missing for the candidate.
Now the test process runs in the same private directory (cwd src/, TMPDIR tmp/, both removed after the
step), so relative paths, tmp_path and gettempdir() mean the same thing to both; the grading workspace
stays out of the candidate's Landlock domain.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

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
'''

_TESTS = '''
import tempfile
from pathlib import Path

import app


def test_a_file_the_candidate_creates_is_seen():
    app.create("made.txt")
    assert Path("made.txt").read_text() == "x"


def test_a_forbidden_file_cannot_pass_unseen():
    app.create("forbidden")
    assert not Path("forbidden").exists()


def test_a_file_the_test_writes_is_read_by_the_candidate():
    Path("input.txt").write_text("hi")
    assert app.read("input.txt") == "hi"


def test_a_file_the_candidate_deletes_is_gone():
    Path("gone.txt").write_text("x")
    app.remove("gone.txt")
    assert not Path("gone.txt").exists()


def test_tmp_path_is_shared(tmp_path):
    app.write_in(str(tmp_path))
    assert (tmp_path / "out.txt").read_text() == "y"


def test_the_temporary_directory_is_the_same():
    assert app.temp_dir() == tempfile.gettempdir()
'''

_IN_PROCESS = {
    "test_a_file_the_candidate_creates_is_seen": "PASS",
    "test_a_forbidden_file_cannot_pass_unseen": "FAIL",
    "test_a_file_the_test_writes_is_read_by_the_candidate": "PASS",
    "test_a_file_the_candidate_deletes_is_gone": "PASS",
    "test_tmp_path_is_shared": "PASS",
    "test_the_temporary_directory_is_the_same": "PASS",
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


def test_the_test_sees_the_candidates_files_as_in_process(tmp_path: Path) -> None:
    cases, output = _grade(_work(tmp_path))
    expected = {name: ("FAIL" if name in _REFUSED else verdict) for name, verdict in _IN_PROCESS.items()}
    assert {name: verdict for name, (verdict, _detail) in cases.items()} == expected, output[-4000:]
