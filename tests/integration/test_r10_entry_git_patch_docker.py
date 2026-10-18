"""In the real coding sandbox, the published patch is written by the image's git and reproduces the
graded files byte for byte (Codex :325/:334/:355).

The real entry runs in the sandbox image with the job's container flags. A scripted agent makes every
kind of change through its terminal: a text edit, a non-UTF-8 edit, a binary file created and one
modified, an empty file created and one deleted, a non-empty file deleted, a last line without a
newline, a change to the last newline only, CR and U+2028 inside a line. On the host, real `git apply`
of the published patch onto a copy of the baseline must give exactly the graded bytes.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aether_worker.coding_jobs import _agent_patch
from aether_worker.coding_sandbox import container_arguments
from aether_worker.container_runtime import execute_docker
from aether_worker.openhands_image import ensure_openhands_sandbox
from aether_worker.runner import docker_available

from tests.unit.test_openhands_coding import _SocketModel

_TESTS = "import app\n\ndef test_value():\n    assert app.value() == 42\n"

_BASELINE = {
    "app.py": b"def value():\n    return 1\n",
    "latin.txt": b"caf\xe9\n",
    "blob.bin": b"\x00\x01old\xff",
    "gone.txt": b"A = 1\n",
    "empty_gone.py": b"",
    "tail.py": b"X = 1",
    "eol.py": b"X = 1\n",
    "seps.txt": "a\rb \u2028 c\n".encode(),
}
_GRADED = {
    "app.py": b"def value():\n    return 42\n",
    "latin.txt": b"caf\xe8\n",
    "blob.bin": b"\x00\x01new\xff\x00",
    "made.bin": b"\x89PNG\r\n\x1a\n\x00\xff",
    "empty_new.py": b"",
    "tail.py": b"X = 2",
    "eol.py": b"X = 1",
    "seps.txt": "a\rc \u2028 d\n".encode(),
}


def _agent_command() -> str:
    """One terminal command: write _GRADED and delete what it lacks, from the agent's job directory."""
    plan = {
        "write": {name: base64.b64encode(data).decode("ascii") for name, data in _GRADED.items()},
        "delete": sorted(set(_BASELINE) - set(_GRADED)),
    }
    script = (
        "import base64, json, os\n"
        f"plan = json.loads(base64.b64decode({base64.b64encode(json.dumps(plan).encode()).decode('ascii')!r}))\n"
        "for name, data in plan['write'].items():\n"
        "    open(name, 'wb').write(base64.b64decode(data))\n"
        "for name in plan['delete']:\n"
        "    os.remove(name)\n"
    )
    encoded = base64.b64encode(script.encode()).decode("ascii")
    return f"/usr/local/bin/python -c \"import base64; exec(base64.b64decode('{encoded}'))\""


def test_the_image_has_git() -> None:
    if not docker_available():
        pytest.fail("Docker is required for the coding sandbox check")
    image = ensure_openhands_sandbox()
    completed = execute_docker(["run", "--rm", "--network", "none", "--entrypoint", "git", image, "--version"], timeout=120)
    assert completed.returncode == 0 and completed.stdout.startswith("git version "), completed.stdout + completed.stderr


def test_the_published_patch_applies_to_the_graded_bytes_in_the_real_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not docker_available():
        pytest.fail("Docker is required for the coding sandbox check")
    if shutil.which("git") is None:
        pytest.fail("git is required on the host to apply the published patch")
    image = ensure_openhands_sandbox()
    workspace = tmp_path / "work"
    proxy = tmp_path / "proxy"
    workspace.mkdir()
    proxy.mkdir()
    (workspace / "test_app.py").write_text(_TESTS, encoding="utf-8")
    for name, data in _BASELINE.items():
        (workspace / name).write_bytes(data)
    for path in [workspace, *workspace.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    proxy.chmod(0o777)
    monkeypatch.setenv("TMPDIR", str(proxy))
    fake = _SocketModel(terminal_command=_agent_command())
    socket_name = fake.path.name
    fake.start()
    fake.path.chmod(0o777)
    try:
        args = container_arguments(workspace, image, objective="Make value() return 42.", proxy_dir=proxy, job_token="job-token-unit")
        command = ["run", "--rm", *args[1:-4], "-e", f"AETHER_MODEL_SOCKET=/run/aether/proxy/{socket_name}", *args[-4:]]
        completed = execute_docker(command, timeout=300)
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout[-4000:] + completed.stderr[-4000:]
    body = json.loads(line[len("RESULT ") :])
    patch = body.get("patch", "")
    assert body["cases"] == [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}], body
    assert _agent_patch({"result": {"patch": patch}}) == patch, patch
    applied = tmp_path / "applied"
    shutil.copytree(workspace, applied)
    (tmp_path / "published.diff").write_text(patch, encoding="utf-8")
    done = subprocess.run(
        ["git", "apply", "-v", str(tmp_path / "published.diff")], cwd=applied, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr + patch
    found = {path.relative_to(applied).as_posix(): path.read_bytes() for path in applied.rglob("*") if path.is_file()}
    assert found == {**_GRADED, "test_app.py": _TESTS.encode()}, patch
    assert completed.returncode == 0, body
