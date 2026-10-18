"""Root-level helper modules marked in the baseline are grading files (threat model I-1, option 2).

A root-level `.py` file of the BASELINE whose first line starts with `# aether: grading` belongs to
grading, like oracle.py: the entry restores it from the baseline before grading and leaves the
agent's edits of it out of the published patch; the protocol never gives it to candidate code (not
copied, not importable: "part of grading"), so candidate code can neither read its expected values
nor replace its functions. It is judged from the baseline only: the agent removing the marker does
not make it candidate code, and a file the agent adds with the marker gains nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ENTRY_DIR = Path("services/worker/aether_worker").resolve()
_PYTHON = Path("environments/openhands/.venv/bin/python")
_PROTOCOL = _ENTRY_DIR / "coding_protocol.py"
_NONCE = "root helper marker nonce"

_CHECK = "# aether: grading\ndef ok(value):\n    return value == 42\n"
_TEST = "import app\nimport check\n\ndef test_value():\n    assert check.ok(app.value())\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _entry(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    code = f"import sys, json\nfrom pathlib import Path\nsys.path.append({str(_ENTRY_DIR)!r})\nimport openhands_sandbox_entry as entry\n" + snippet
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AETHER_SANDBOX_WORK": str(tmp_path / "work")}
    return subprocess.run([str(_PYTHON.absolute()), "-I", "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)


def _restored(tmp_path: Path, baseline: dict[str, str], job: dict[str, str | None]) -> dict[str, str]:
    """Run the entry's restore on a job directory: job maps name -> the agent's text, or None for deleted."""
    work, directory = tmp_path / "work", tmp_path / "job"
    _write(work, baseline)
    _write(directory, {name: text for name, text in baseline.items()})
    for name, text in job.items():
        if text is None:
            (directory / name).unlink()
        else:
            _write(directory, {name: text})
    completed = _entry(
        "entry.restore_grading_directory(Path('work'), Path('job'))\n"
        "print(json.dumps({p.relative_to('job').as_posix(): p.read_text() for p in Path('job').rglob('*') if p.is_file()}))\n",
        tmp_path,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


_BASELINE = {"app.py": "def value():\n    return 41\n", "check.py": _CHECK, "test_app.py": _TEST}


def test_an_edited_marked_helper_is_restored_even_with_the_marker_removed(tmp_path: Path) -> None:
    found = _restored(tmp_path, _BASELINE, {"check.py": "def ok(value):\n    return True\n"})
    assert found["check.py"] == _CHECK


def test_a_deleted_marked_helper_is_restored(tmp_path: Path) -> None:
    found = _restored(tmp_path, _BASELINE, {"check.py": None})
    assert found["check.py"] == _CHECK


def test_a_new_file_with_the_marker_stays_the_agents_source(tmp_path: Path) -> None:
    added = "# aether: grading\nHELPER = 1\n"
    found = _restored(tmp_path, _BASELINE, {"helper.py": added, "app.py": "def value():\n    return 42\n"})
    assert found["helper.py"] == added
    assert found["app.py"] == "def value():\n    return 42\n"
    assert found["check.py"] == _CHECK


def test_the_agents_edit_of_a_marked_helper_is_not_in_the_published_patch(tmp_path: Path) -> None:
    work, directory = tmp_path / "work", tmp_path / "job"
    _write(work, _BASELINE)
    _write(directory, _BASELINE)
    completed = _entry(
        "before = entry._snapshot(Path('job'))\n"
        "Path('job/check.py').write_text('def ok(value):\\n    return True\\n')\n"
        "Path('job/app.py').write_text('def value():\\n    return 42\\n')\n"
        "print(json.dumps(entry._patch(before, Path('job'))))\n",
        tmp_path,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    patch = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "app.py" in patch
    assert "check.py" not in patch, patch


def _grade(baseline: Path, job: Path) -> tuple[int, dict | None, str]:
    secret = job.parent / (job.name + ".nonce")
    secret.write_text(_NONCE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env.update({"AETHER_PROTOCOL_NONCE_FILE": str(secret), "AETHER_PROTOCOL_SHM_DIR": "/dev/shm", "AETHER_PROTOCOL_BASELINE_DIR": str(baseline)})
    completed = subprocess.run([sys.executable, "-I", str(_PROTOCOL)], cwd=job, env=env, capture_output=True, text=True, timeout=60, check=False)
    signed = []
    for line in completed.stdout.splitlines():
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("nonce") == _NONCE:
            signed.append(body)
    return completed.returncode, (signed[0] if len(signed) == 1 else None), completed.stdout + completed.stderr


def _graded(tmp_path: Path, job_files: dict[str, str], baseline: dict[str, str] | None = None) -> tuple[int, dict, str]:
    base, job = tmp_path / "baseline", tmp_path / "job"
    _write(base, _BASELINE if baseline is None else baseline)
    _write(job, job_files)
    code, body, output = _grade(base, job)
    assert body is not None, output
    return code, body, output


@pytest.mark.parametrize(
    "app",
    [
        "import check\ncheck.ok = lambda value: True\n\ndef value():\n    return 41\n",
        "def value():\n    import check\n    return 42 if check.ok(42) else 0\n",
        "def value():\n    return 42 if 'value == 42' in open('check.py').read() else 0\n",
    ],
    ids=["replaces_the_helper", "imports_the_helper", "reads_the_helper"],
)
def test_candidate_code_cannot_use_a_marked_helper(tmp_path: Path, app: str) -> None:
    code, body, output = _graded(tmp_path, {**_BASELINE, "app.py": app})
    assert code == 1, (body, output[-2000:])
    assert body["passed"] == 0, body


def test_a_marked_helper_is_judged_from_the_baseline_not_the_job_copy(tmp_path: Path) -> None:
    # Even if a job copy lacked the marker, the baseline decides; candidate code still cannot replace it.
    app = "import check\ncheck.ok = lambda value: True\n\ndef value():\n    return 41\n"
    code, body, output = _graded(tmp_path, {**_BASELINE, "check.py": _CHECK.replace("# aether: grading\n", ""), "app.py": app})
    assert code == 1, (body, output[-2000:])
    assert body["passed"] == 0, body


def test_a_correct_candidate_passes_with_a_marked_helper(tmp_path: Path) -> None:
    code, body, output = _graded(tmp_path, {**_BASELINE, "app.py": "def value():\n    return 42\n"})
    assert body["cases"] == [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}], (body, output[-2000:])
    assert code == 0


def test_a_new_file_with_the_marker_is_still_candidate_code(tmp_path: Path) -> None:
    files = {**_BASELINE, "helper.py": "# aether: grading\nANSWER = 42\n", "app.py": "import helper\n\ndef value():\n    return helper.ANSWER\n"}
    code, body, output = _graded(tmp_path, files)
    assert body["cases"] == [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}], (body, output[-2000:])
    assert code == 0


def test_only_root_level_python_files_whose_first_line_is_the_marker_are_marked(tmp_path: Path) -> None:
    sys.path.append(str(_ENTRY_DIR))
    try:
        import grading_files
    finally:
        sys.path.remove(str(_ENTRY_DIR))
    _write(
        tmp_path,
        {
            "check.py": _CHECK,
            "spaced.py": "# aether: grading   \nX = 1\n",
            "second_line.py": "X = 1\n# aether: grading\n",
            "pkg/inner.py": _CHECK,
            "notes.txt": _CHECK,
            "lookalike.py": "# aether: grading-ish\nX = 1\n",
        },
    )
    (tmp_path / "linked.py").symlink_to(tmp_path / "check.py")
    assert grading_files.marked_grading_files(tmp_path) == frozenset({"check.py", "spaced.py"})
