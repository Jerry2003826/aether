"""Candidate files past the 8 MiB cap fail the run closed (Codex coding_protocol.py:819).

Leaving the later files out and grading anyway grades a tree that is not the published one: a test
could pass against a candidate missing files the patch contains. Past the cap the protocol runs no
test and reports one suite failure, candidate_files_too_large; what was over the cap is logged by
name and count (never content) on the grader's stderr.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.test_r10_oracle_isolation import _grade, _write

_MARKER = "candidate file content marker"


def test_candidate_files_over_the_cap_fail_the_run_and_run_no_test(tmp_path: Path) -> None:
    mib = 1024 * 1024
    line = (_MARKER + "\n").encode()
    (tmp_path / "big1.bin").write_bytes(line * (5 * mib // len(line)))
    (tmp_path / "big2.bin").write_bytes(line * (5 * mib // len(line)))
    _write(
        tmp_path,
        {
            "app.py": "def value():\n    return 42\n",
            "test_cap.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    code, body, output = _grade(tmp_path)
    assert body is not None, output
    assert body["cases"] == [{"name": "suite", "verdict": "FAIL", "detail": "candidate_files_too_large"}], body
    assert body["passed"] == 0 and body["failed"] == 1, body
    assert code != 0, body
    assert "big2.bin" in output
    assert _MARKER not in output


def test_candidate_files_at_the_cap_are_all_copied(tmp_path: Path) -> None:
    mib = 1024 * 1024
    (tmp_path / "data.bin").write_bytes(b"d" * (8 * mib - 4096))
    _write(
        tmp_path,
        {
            "app.py": "import os\n\ndef size():\n    return os.path.getsize('data.bin')\n",
            "test_cap.py": f"import app\n\ndef test_size():\n    assert app.size() == {8 * mib - 4096}\n",
        },
    )
    code, body, output = _grade(tmp_path)
    assert body is not None, output
    assert body["cases"] == [{"name": "test_cap.test_size", "verdict": "PASS", "detail": ""}], body
    assert code == 0, body
