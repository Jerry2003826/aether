"""Every file of the trusted `tests` map is grading, whatever its name (Codex r4308195942).

A coding workspace has `files` (the agent's) and `tests` (trusted). A root-level helper in `tests` such
as `expected.py` is neither a test file by name, a support file, nor marked with `# aether: grading`;
before, the entry left the agent's edits of it in place and the protocol ran it as candidate code, so
an agent could change an expected constant and make a bad patch pass. The worker now writes the
complete trusted path set into the baseline as `.aether-grading.json` (never a workspace path: those
end in .py), and the grading classification reads it from the BASELINE: each listed file is restored
before grading, kept out of the published patch, and never given to candidate code. The manifest
itself is grading too. A manifest that is not a list of relative paths fails closed.
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
_NONCE = "trusted helper grading nonce"
_MANIFEST = ".aether-grading.json"

_EXPECTED = "ANSWER = 42\n"
_TEST = "import app\nimport expected\n\ndef test_value():\n    assert app.value() == expected.ANSWER\n"
_TESTS = {"test_app.py": _TEST, "expected.py": _EXPECTED}
_BASELINE = {"app.py": "def value():\n    return 41\n", **_TESTS, _MANIFEST: json.dumps(sorted(_TESTS))}


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


def _restored(tmp_path: Path, job: dict[str, str | None], baseline: dict[str, str] = _BASELINE) -> dict[str, str]:
    work, directory = tmp_path / "work", tmp_path / "job"
    _write(work, baseline)
    _write(directory, baseline)
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


def test_an_edited_trusted_helper_is_restored(tmp_path: Path) -> None:
    found = _restored(tmp_path, {"expected.py": "ANSWER = 41\n", "app.py": "def value():\n    return 41\n"})
    assert found["expected.py"] == _EXPECTED


def test_a_deleted_trusted_helper_is_restored(tmp_path: Path) -> None:
    assert _restored(tmp_path, {"expected.py": None})["expected.py"] == _EXPECTED


def test_an_edited_manifest_is_restored(tmp_path: Path) -> None:
    found = _restored(tmp_path, {_MANIFEST: "[]"})
    assert json.loads(found[_MANIFEST]) == sorted(_TESTS)


def test_the_agents_files_stay_the_agents(tmp_path: Path) -> None:
    assert _restored(tmp_path, {"app.py": "def value():\n    return 42\n"})["app.py"] == "def value():\n    return 42\n"


def test_edits_of_the_trusted_helper_and_manifest_are_not_in_the_published_patch(tmp_path: Path) -> None:
    work, directory = tmp_path / "work", tmp_path / "job"
    _write(work, _BASELINE)
    _write(directory, _BASELINE)
    completed = _entry(
        "before = entry._snapshot(Path('job'))\n"
        "Path('job/expected.py').write_text('ANSWER = 41\\n')\n"
        f"Path('job/{_MANIFEST}').write_text('[]')\n"
        "Path('job/app.py').write_text('def value():\\n    return 42\\n')\n"
        "print(json.dumps(entry._patch(before, Path('job'))))\n",
        tmp_path,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    patch = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "app.py" in patch
    assert "expected.py" not in patch and _MANIFEST not in patch, patch


def _grade(baseline: Path, job: Path) -> tuple[dict | None, str]:
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
    return (signed[0] if len(signed) == 1 else None), completed.stdout + completed.stderr


def _verdicts(tmp_path: Path, app: str) -> list[dict]:
    base, job = tmp_path / "baseline", tmp_path / "job"
    _write(base, _BASELINE)
    _write(job, {**_BASELINE, "app.py": app})
    body, output = _grade(base, job)
    assert body is not None, output[-3000:]
    return body["cases"]


@pytest.mark.parametrize(
    "app",
    [
        "def value():\n    import expected\n    return expected.ANSWER\n",
        "def value():\n    return int(open('expected.py').read().split('=')[1])\n",
    ],
    ids=["imports_the_helper", "reads_the_helper"],
)
def test_candidate_code_cannot_use_a_trusted_helper(tmp_path: Path, app: str) -> None:
    cases = _verdicts(tmp_path, app)
    assert [case["verdict"] for case in cases] == ["FAIL"], cases


def test_a_correct_candidate_passes(tmp_path: Path) -> None:
    assert _verdicts(tmp_path, "def value():\n    return 42\n") == [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}]


def _grading_files():
    sys.path.append(str(_ENTRY_DIR))
    try:
        import grading_files
    finally:
        sys.path.remove(str(_ENTRY_DIR))
    return grading_files


def test_the_classification_reads_the_manifest_from_the_baseline(tmp_path: Path) -> None:
    files = _grading_files()
    _write(tmp_path, _BASELINE)
    marked = files.marked_grading_files(tmp_path)
    assert {"expected.py", "test_app.py", _MANIFEST} <= marked
    assert "app.py" not in marked
    assert files.is_grading_path("expected.py", marked=marked)
    assert files.is_grading_path(_MANIFEST)


@pytest.mark.parametrize("text", ["{}", "not json", '["../x.py"]', '["/abs.py"]', "[1]", '["a.txt"]'])
def test_an_invalid_manifest_fails_closed(tmp_path: Path, text: str) -> None:
    files = _grading_files()
    _write(tmp_path, {"app.py": "X = 1\n", _MANIFEST: text})
    with pytest.raises(ValueError, match="grading_manifest_invalid"):
        files.marked_grading_files(tmp_path)


def test_the_worker_writes_the_trusted_path_set_and_does_not_publish_it(tmp_path: Path) -> None:
    from aether_worker import coding_jobs

    workspace = {"base_files": {}, "files": {"app.py": "X = 1\n"}, "tests": {"test_app.py": _TEST, "expected.py": _EXPECTED}}
    coding_jobs._materialize(workspace, tmp_path)
    assert json.loads((tmp_path / _MANIFEST).read_text()) == ["expected.py", "test_app.py"]
    assert coding_jobs._manifest(tmp_path, workspace) == ["app.py", "expected.py", "test_app.py"]
