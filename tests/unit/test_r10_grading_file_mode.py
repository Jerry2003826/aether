"""A trusted grading file keeps its baseline mode, not only its baseline bytes (Codex 09:35, entry:813).

The sandbox entry copies the baseline into the job directory with the one mode bit git records
(0o755 for an executable file, 0o644 otherwise). Before, restore_grading_directory skipped any
grading file whose bytes still matched, and _verify_grading_directory compared bytes only, so an
agent that only ran chmod on a trusted test or fixture (made it world-writable, or dropped or added
the executable bit) kept that mode in the directory that is then graded. Every baseline grading
file now gets the baseline mode back, a rewritten file too, and the check compares the mode as well.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_ENTRY_DIR = Path("services/worker/aether_worker").resolve()
_PYTHON = Path("environments/openhands/.venv/bin/python")

_TEST = "import app\n\ndef test_value():\n    assert app.value() == 42\n"
_SCRIPT = "#!/bin/sh\necho trusted fixture script\n"
_BASELINE = {"app.py": "def value():\n    return 41\n", "tests/test_app.py": _TEST, "tests/run_fixture.sh": _SCRIPT}
_MODES = {"app.py": 0o644, "tests/test_app.py": 0o644, "tests/run_fixture.sh": 0o755}


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(_MODES[name])


def _entry(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    code = (
        "import sys, json, os, stat\nfrom pathlib import Path\n"
        f"sys.path.append({str(_ENTRY_DIR)!r})\nimport openhands_sandbox_entry as entry\n" + snippet
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AETHER_SANDBOX_WORK": str(tmp_path / "work")}
    return subprocess.run([str(_PYTHON.absolute()), "-I", "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)


def _restored_modes(tmp_path: Path, modes: dict[str, int], edits: dict[str, str] | None = None) -> dict[str, list]:
    _write(tmp_path / "work", _BASELINE)
    _write(tmp_path / "job", _BASELINE)
    for name, text in (edits or {}).items():
        (tmp_path / "job" / name).write_text(text, encoding="utf-8")
    for name, mode in modes.items():
        (tmp_path / "job" / name).chmod(mode)
    completed = _entry(
        "os.umask(0o077)\n"
        "entry.restore_grading_directory(Path('work'), Path('job'))\n"
        "print(json.dumps({p.relative_to('job').as_posix(): [stat.S_IMODE(p.stat().st_mode), p.read_text()]"
        " for p in Path('job').rglob('*') if p.is_file()}))\n",
        tmp_path,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("name", "mode"),
    [("tests/test_app.py", 0o666), ("tests/test_app.py", 0o755), ("tests/test_app.py", 0o600), ("tests/run_fixture.sh", 0o644), ("tests/run_fixture.sh", 0o777)],
    ids=["test-made-world-writable", "test-made-executable", "test-made-owner-only", "script-lost-executable", "script-made-world-writable"],
)
def test_a_chmod_only_change_of_a_trusted_file_is_restored(tmp_path: Path, name: str, mode: int) -> None:
    found = _restored_modes(tmp_path, {name: mode})
    assert found[name] == [_MODES[name], _BASELINE[name]], f"{name} kept mode {oct(found[name][0])}"


def test_a_rewritten_executable_trusted_file_gets_its_executable_mode_back(tmp_path: Path) -> None:
    found = _restored_modes(tmp_path, {"tests/run_fixture.sh": 0o600}, {"tests/run_fixture.sh": "#!/bin/sh\necho agent edit\n"})
    assert found["tests/run_fixture.sh"] == [0o755, _SCRIPT]


def test_the_agents_own_file_keeps_the_agents_mode(tmp_path: Path) -> None:
    assert _restored_modes(tmp_path, {"app.py": 0o755})["app.py"][0] == 0o755


def test_a_mode_the_restore_could_not_fix_fails_the_check(tmp_path: Path) -> None:
    _write(tmp_path / "work", _BASELINE)
    _write(tmp_path / "job", _BASELINE)
    (tmp_path / "job" / "tests" / "test_app.py").chmod(0o666)
    completed = _entry(
        "os.chmod = lambda *args, **kwargs: None\n"
        "try:\n    entry.restore_grading_directory(Path('work'), Path('job'))\n"
        "except RuntimeError as exc:\n    print('raised', exc)\nelse:\n    print('no error')\n",
        tmp_path,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    assert completed.stdout.strip().splitlines()[-1] == "raised grading_mode_mismatch"
