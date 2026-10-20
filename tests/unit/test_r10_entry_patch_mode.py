"""The published patch keeps the executable bit: the entry's copy of the baseline keeps file modes,
and a mode change (alone or with content) is a change in the patch. `git apply` of the patch onto a
fresh baseline copy reproduces every file's bytes and executable bit."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

_ENTRY_DIR = Path("services/worker/aether_worker").resolve()
_PYTHON = Path("environments/openhands/.venv/bin/python")

_BASELINE = {
    "app.py": ("def value():\n    return 1\n", 0o644),
    "run.sh": ("#!/bin/sh\necho run\n", 0o755),
    "tool.py": ("#!/usr/bin/env python3\nprint(1)\n", 0o755),
}


def _write(root: Path, files: dict[str, tuple[str, int]]) -> None:
    for name, (text, mode) in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)


def _entry(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    code = f"import sys\nfrom pathlib import Path\nsys.path.append({str(_ENTRY_DIR)!r})\nimport openhands_sandbox_entry as entry\n" + snippet
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AETHER_SANDBOX_WORK": str(tmp_path / "work")}
    return subprocess.run([str(_PYTHON.absolute()), "-I", "-c", code], cwd=tmp_path, env=env, capture_output=True, timeout=120, check=False)


def _executable(path: Path) -> bool:
    return bool(stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR)


def _state(root: Path) -> dict[str, tuple[bytes, bool]]:
    return {p.relative_to(root).as_posix(): (p.read_bytes(), _executable(p)) for p in sorted(root.rglob("*")) if p.is_file()}


def _published(tmp_path: Path, change: str) -> tuple[str, Path]:
    """Copy the baseline with the entry's copy_tree, snapshot, apply `change`, return the patch."""
    _write(tmp_path / "work", _BASELINE)
    snippet = (
        "work, job = Path('work'), Path('job')\n"
        "entry.copy_tree(work, job)\n"
        "before = entry._snapshot(job)\n" + change + "sys.stdout.write(entry._patch(before, job))\n"
    )
    completed = _entry(snippet, tmp_path)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.decode(), tmp_path / "job"


def test_the_entrys_copy_keeps_the_executable_bit(tmp_path: Path) -> None:
    _write(tmp_path / "work", _BASELINE)
    completed = _entry("entry.copy_tree(Path('work'), Path('job'))\n", tmp_path)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert _state(tmp_path / "job") == _state(tmp_path / "work")


_CHANGES = {
    "make_executable": "(job / 'app.py').chmod(0o755)\n",
    "drop_executable": "(job / 'run.sh').chmod(0o644)\n",
    "new_executable": "(job / 'new.sh').write_text('#!/bin/sh\\necho new\\n')\n(job / 'new.sh').chmod(0o755)\n",
    "content_and_mode": "(job / 'tool.py').write_text('#!/usr/bin/env python3\\nprint(2)\\n')\n(job / 'tool.py').chmod(0o644)\n",
}


@pytest.mark.parametrize("change", list(_CHANGES.values()), ids=list(_CHANGES))
def test_git_apply_of_the_patch_reproduces_bytes_and_mode(tmp_path: Path, change: str) -> None:
    patch, job = _published(tmp_path, change)
    assert patch, "a mode change is a change"
    target = tmp_path / "applied"
    _write(target, _BASELINE)
    (tmp_path / "published.diff").write_text(patch, encoding="utf-8")
    done = subprocess.run(["git", "apply", "-v", str(tmp_path / "published.diff")], cwd=target, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr + patch
    assert _state(target) == _state(job), patch


def test_an_untouched_executable_file_is_not_a_change(tmp_path: Path) -> None:
    patch, _job = _published(tmp_path, "")
    assert patch == ""
