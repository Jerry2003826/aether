"""In the real coding sandbox container, candidate code cannot read tests or grading files.

Same container flags as a coding job (coding_sandbox.container_arguments: read-only rootfs, no network,
cap-drop ALL, no-new-privileges, uid 65534, the workspace on /work read-only) and the sandbox image
built from this checkout; the protocol runs the way the entry runs it, in a private job dir under /tmp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aether_worker.coding_sandbox import container_arguments
from aether_worker.container_runtime import execute_docker
from aether_worker.openhands_image import ensure_openhands_sandbox
from aether_worker.runner import docker_available

from tests.unit.test_r10_candidate_files import (
    _APP,
    _HELPER_FILES,
    _HELPER_LEAKS,
    _LEAKS,
    _TESTS,
)

_NONCE = "n0nce-docker-candidate-files"
_DRIVER = (
    "import json, os, shutil, subprocess, sys, tempfile\n"
    "job = tempfile.mkdtemp(prefix='aether-job-')\n"
    "shutil.copytree('/work', job, dirs_exist_ok=True)\n"
    "secret = os.path.join(tempfile.mkdtemp(prefix='aether-nonce-'), 'nonce')\n"
    f"open(secret, 'w').write({_NONCE!r})\n"
    "env = {k: v for k, v in os.environ.items() if not k.startswith('AETHER_')}\n"
    "env['AETHER_PROTOCOL_NONCE_FILE'] = secret\n"
    "done = subprocess.run([sys.executable, '-I', '/opt/aether/coding_protocol.py'], cwd=job, env=env,\n"
    "                      capture_output=True, text=True, timeout=200)\n"
    "print(json.dumps({'exit': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr[-4000:]}))\n"
)


def _grade_in_sandbox(workspace: Path) -> tuple[int, dict]:
    if not docker_available():
        pytest.fail("Docker is required for the coding sandbox check")
    image = ensure_openhands_sandbox()
    args = container_arguments(workspace, image, objective="grade only")
    # Same flags; only the command differs: the protocol instead of the agent entry.
    assert args[-4:] == [image, "/opt/openhands/.venv/bin/python", "-I", "/opt/aether/openhands_sandbox_entry.py"]
    completed = execute_docker(["run", "--rm", *args[1:-3], "/opt/openhands/.venv/bin/python", "-I", "-c", _DRIVER], timeout=300)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    signed = [json.loads(line) for line in report["stdout"].splitlines() if _NONCE in line]
    assert len(signed) == 1, report
    return report["exit"], signed[0]


def test_candidate_cannot_read_tests_in_the_coding_sandbox(tmp_path: Path) -> None:
    app = _APP + (
        "\ndef baseline():\n"
        "    # The read-only baseline mount holds the tests too.\n"
        "    return secret('/work/test_secret.py'), listing('/work')\n"
    )
    tests = _TESTS + (
        "\ndef test_baseline_mount():\n"
        "    assert app.baseline()[0] == EXPECTED\n"
        "\ndef test_baseline_mount_denied():\n"
        "    assert app.baseline() == (-1, None)\n"
    )
    for name, text in {"app.py": app, "data.txt": "data\n", "test_secret.py": tests}.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    tmp_path.chmod(0o755)
    for path in tmp_path.iterdir():
        path.chmod(0o644)
    code, body = _grade_in_sandbox(tmp_path)
    verdicts = {case["name"]: case["verdict"] for case in body["cases"]}
    assert code == 1, body
    for name in (*_LEAKS, "test_secret.test_baseline_mount"):
        assert verdicts.get(name) == "FAIL", (name, body)
    for name in ("test_reads_are_denied", "test_candidate_files_still_work", "test_baseline_mount_denied"):
        assert verdicts.get("test_secret." + name) == "PASS", (name, body)


def test_grading_helpers_are_not_readable_in_the_coding_sandbox(tmp_path: Path) -> None:
    for name, text in _HELPER_FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for path in [tmp_path, *tmp_path.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    code, body = _grade_in_sandbox(tmp_path)
    verdicts = {case["name"]: case["verdict"] for case in body["cases"]}
    assert code == 1, body
    for name in _HELPER_LEAKS:
        assert verdicts.get(name) == "FAIL", (name, body)
    assert verdicts.get("tests.test_helper.test_helper_is_denied") == "PASS", body
