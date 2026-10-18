"""The worker's candidate patch is written by the git on the worker's own PATH, end to end.

unified_patch runs in the worker process (`python -m aether_worker` -> service -> coding_jobs), which
runs on the host from the project environment; there is no worker image. So this runs it the way the
worker does: a fresh interpreter of this environment with the worker's PATH, then `git apply` of the
patch it wrote on a copy of the base files and a byte-for-byte comparison with the candidate files.
Git is a worker prerequisite: when it is missing the first test fails rather than skips. A worker
whose PATH has no git gets patch_unavailable, and the candidate patch falls back to the unchanged
marker with that reason instead of raising.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.unit.test_p1_git_patch_format import CASES, _git_apply, _raw

_ROOT = Path(__file__).resolve().parents[2]
_PYTHONPATH = os.pathsep.join(
    str(_ROOT / part) for part in (".", "packages/domain", "packages/storage", "services/api", "services/gateway", "services/worker")
)
_DRIVER = (
    "import base64, json, sys\n"
    "from aether_worker.coding_jobs import _candidate_patch\n"
    "from aether_worker.coding_workspace import WorkspaceError, unified_patch\n"
    "cases = json.loads(sys.stdin.read())\n"
    "def load(files):\n"
    "    return {k: base64.b64decode(v) for k, v in files.items()}\n"
    "out = {}\n"
    "for name, (base, files) in cases.items():\n"
    "    try:\n"
    "        out[name] = {'patch': unified_patch(load(base), load(files))}\n"
    "    except WorkspaceError as exc:\n"
    "        out[name] = {'error': exc.code}\n"
    "    patch, error = _candidate_patch({'base_files': load(base), 'files': load(files)}, job_id='job-it')\n"
    "    out[name]['fallback'] = [patch.decode('utf-8'), error]\n"
    "print(json.dumps(out))\n"
)


def _run_worker_interpreter(path: str, tmp_path: Path) -> dict[str, dict]:
    payload = {
        name: [{k: base64.b64encode(v).decode() for k, v in _raw(side).items()} for side in pair]
        for name, pair in CASES.items()
    }
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    completed = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path[:0] = {_PYTHONPATH.split(os.pathsep)!r}\n" + _DRIVER],
        input=json.dumps(payload),
        env={"PATH": path, "HOME": str(tmp_path), "TMPDIR": str(scratch), "LC_ALL": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout)


def test_the_worker_writes_the_patch_with_its_git_and_git_apply_reproduces_the_files(tmp_path: Path) -> None:
    worker_path = os.environ.get("PATH", os.defpath)
    assert shutil.which("git", path=worker_path), "git is a worker prerequisite and is not on PATH"
    results = _run_worker_interpreter(worker_path, tmp_path)
    assert sorted(results) == sorted(CASES)
    for name, (base, files) in sorted(CASES.items()):
        assert "error" not in results[name], (name, results[name])
        patch = results[name]["patch"]
        assert patch.startswith("diff --git "), (name, patch)
        assert results[name]["fallback"] == [patch, None]
        assert _git_apply(_raw(base), patch, tmp_path / f"applied-{name}") == _raw(files), name


def test_a_worker_without_git_gets_patch_unavailable_and_the_unchanged_marker(tmp_path: Path) -> None:
    bare = tmp_path / "bin-without-git"
    bare.mkdir()
    results = _run_worker_interpreter(str(bare), tmp_path)
    for name in CASES:
        assert results[name]["error"] == "patch_unavailable", (name, results[name])
        assert results[name]["fallback"] == ["--- a/empty\n+++ b/empty\n", "patch_unavailable"], name

