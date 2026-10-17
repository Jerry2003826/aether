"""The entry's RESULT line always reaches the worker whole, and empty-file changes are changes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aether_worker import coding_sandbox
from aether_worker.coding_jobs import _agent_patch, _coding_terminal

_ENTRY_DIR = Path("services/worker/aether_worker").resolve()
_PYTHON = Path("environments/openhands/.venv/bin/python")


def _entry(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    """Run `snippet` with the sandbox entry imported as `entry`, in the sandbox's interpreter."""
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    code = (
        f"import sys\nsys.path.append({str(_ENTRY_DIR)!r})\nimport openhands_sandbox_entry as entry\n"
        + snippet
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AETHER_SANDBOX_WORK": str(tmp_path / "work"),
    }
    return subprocess.run(
        [str(_PYTHON.absolute()), "-I", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_a_patch_that_json_escaping_inflates_still_yields_a_whole_result_line(
    tmp_path: Path, monkeypatch
) -> None:
    # Under the raw 2 MiB cap, but each control character becomes a six-byte \u00XX escape.
    raw = 1_900_000
    assert raw < coding_sandbox.MAX_PATCH_BYTES
    snippet = (
        f"patch = '--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n+' + '\\x01' * {raw} + '\\n'\n"
        "entry.emit({'passed': 1, 'failed': 0, 'errors': 0, 'cases': [{'name': 'test_app.test_value', 'verdict': 'PASS', 'detail': ''}],"
        " 'agent_completed': True, 'model_requests': 1, 'patch': patch}, 0)\n"
    )
    completed = _entry(snippet, tmp_path)
    assert completed.returncode == 0, completed.stderr[-2000:]
    stdout = b"agent log line\n" * 1000 + completed.stdout
    line = stdout.rstrip(b"\n").rsplit(b"\n", 1)[-1]
    assert line.startswith(b"RESULT ")
    assert len(line) + 1 <= coding_sandbox.RESULT_CAPTURE_LIMIT, len(line)

    # What the worker reads back: only the last RESULT_CAPTURE_LIMIT bytes of stdout.
    monkeypatch.setattr(
        coding_sandbox, "container_logs", lambda _name, *, limit: (stdout[-limit:], b"")
    )
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(
        coding_sandbox,
        "_sandbox_docker",
        lambda args, *, timeout=None: subprocess.CompletedProcess(
            args, 0, "0\n" if args[:2] == ["inspect", "-f"] else "", ""
        ),
    )
    collected = coding_sandbox._collect_exit("aether-code-escape")
    result = collected["result"]
    assert isinstance(result, dict), (
        "the RESULT line was cut and the worker would report sandbox_failed"
    )
    assert result["patch"] == ""
    assert result["patch_omitted"] == "too_large"
    sandbox = {"exit_code": 0, "result": result}
    assert _coding_terminal(
        sandbox, upstream_error=None, measured=(3, 4), usage_missing=False
    ) == ("FAILED", "patch_too_large")


def _patch_after(tmp_path: Path, baseline: dict[str, str], change: str) -> str:
    job = tmp_path / "job"
    for name, text in baseline.items():
        path = job / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    snippet = (
        "from pathlib import Path\n"
        f"job = Path({str(job)!r})\n"
        "before = entry._snapshot(job)\n"
        + change
        + "sys.stdout.write(entry._patch(before, job))\n"
    )
    completed = _entry(snippet, tmp_path)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.decode()


@pytest.mark.parametrize(
    ("baseline", "change", "name"),
    [
        (
            {"app.py": "def value():\n    return 1\n"},
            "(job / 'pkg').mkdir()\n(job / 'pkg' / '__init__.py').write_text('')\n",
            "pkg/__init__.py",
        ),
        (
            {"app.py": "def value():\n    return 1\n", "legacy.py": ""},
            "(job / 'legacy.py').unlink()\n",
            "legacy.py",
        ),
    ],
    ids=["create_empty", "delete_empty"],
)
def test_creating_or_deleting_an_empty_file_is_a_change(
    tmp_path: Path, baseline: dict[str, str], change: str, name: str
) -> None:
    patch = _patch_after(tmp_path, baseline, change)
    assert name in patch, patch
    sandbox = {"exit_code": 0, "result": {"patch": patch}}
    assert _agent_patch(sandbox) == patch


def test_an_untouched_empty_file_is_not_a_change(tmp_path: Path) -> None:
    assert (
        _patch_after(tmp_path, {"app.py": "x = 1\n", "pkg/__init__.py": ""}, "") == ""
    )
