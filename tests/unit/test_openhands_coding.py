"""The coding entry calls OpenHands LocalWorkspace. There is no importlib fallback."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from aether_worker.coding_sandbox import container_arguments
from aether_worker.openhands_image import OPENHANDS_VERSION, launch_image
from aether_worker.runner import JOB_IMAGE
from aether_worker.runner_process import _ensure_coding_image

_ENTRY = Path("services/worker/aether_worker/openhands_sandbox_entry.py")
_PYTHON = Path("environments/openhands/.venv/bin/python")


def test_entry_source_calls_local_workspace_and_launch_keeps_other_images(monkeypatch) -> None:
    tree = ast.parse(_ENTRY.read_text(encoding="utf-8"))
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    imported.extend(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "openhands.sdk.workspace.local" in imported
    assert "execute_command" in calls
    assert "mkdtemp" in calls
    assert "/tmp" not in _ENTRY.read_text(encoding="utf-8")
    assert not any(name == "importlib" or name.startswith("importlib.") for name in imported)
    assert launch_image("python:3.12-alpine", JOB_IMAGE) == "python:3.12-alpine"
    monkeypatch.setattr("aether_worker.openhands_image.coding_runtime_image", lambda: None)
    try:
        launch_image(JOB_IMAGE, JOB_IMAGE)
    except LookupError as exc:
        assert str(exc) == "image_unavailable"
    else:
        raise AssertionError("missing sandbox image was accepted")
    args = container_arguments(Path("."), JOB_IMAGE)
    assert args[-1] == "/opt/aether/openhands_sandbox_entry.py"
    assert "importlib" not in " ".join(args)


def test_local_workspace_strips_its_secret_before_the_protocol(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    protocol = tmp_path / "protocol.py"
    protocol.write_text(
        "import json, os, sys\n"
        "body = {"
        "'passed': 1, 'failed': 0, 'errors': 0, "
        "'cases': [{'name': 'suite.test_value', 'verdict': 'PASS', 'detail': ''}], "
        "'elapsed_seconds': 0, "
        "'oh_secret_visible': 'OH_SECRET_KEY' in os.environ, "
        "'ai_agent': os.environ.get('AI_AGENT', '')}\n"
        "sys.stdout.write(json.dumps(body, separators=(',', ':')) + '\\n')\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    (work / "app.py").write_text("def value():\n    return 42\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "OH_SECRET_KEY": "should-not-pass",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_SANDBOX_PROTOCOL": str(protocol),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert completed.returncode == 0 and line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    assert body["executor"] == "openhands.sdk.LocalWorkspace"
    assert body["passed"] == 1
    assert body["failed"] == 0
    assert body["oh_secret_visible"] is False
    assert body["ai_agent"] == "openhands"
    assert body["credential_present"] is True
    assert isinstance(body["network_errno"], int)
    assert OPENHANDS_VERSION == "1.54.0"
    assert sys.version_info >= (3, 13)


def test_runner_builds_the_openhands_image_when_docker_is_enabled(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("aether_worker.runner_process.coding_runtime_image", lambda: None)

    def build() -> str:
        calls.append("build")
        return "sha256:built"

    monkeypatch.setattr("aether_worker.runner_process.build_openhands_sandbox", build)
    _ensure_coding_image({"runtime": "enabled"})
    assert calls == ["build"]
    calls.clear()
    monkeypatch.setattr("aether_worker.runner_process.coding_runtime_image", lambda: "sha256:present")
    _ensure_coding_image({"runtime": "enabled"})
    _ensure_coding_image({"runtime": "unsupported", "reason": "cli_missing"})
    assert calls == []


def test_runner_stays_up_when_the_openhands_image_build_fails(monkeypatch) -> None:
    def boom() -> str:
        raise OSError("disk")

    monkeypatch.setattr("aether_worker.runner_process.coding_runtime_image", lambda: None)
    monkeypatch.setattr("aether_worker.runner_process.build_openhands_sandbox", boom)
    _ensure_coding_image({"runtime": "enabled"})
