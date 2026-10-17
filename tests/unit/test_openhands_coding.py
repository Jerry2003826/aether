"""The coding entry calls the OpenHands agent. There is no importlib fallback."""

from __future__ import annotations

import ast
import difflib
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from aether_worker.coding_sandbox import CODING_MEMORY_BYTES, container_arguments
from aether_worker.openhands_image import OPENHANDS_VERSION, launch_image
from aether_worker.runner import JOB_IMAGE
from aether_worker.runner_process import _ensure_coding_image

_ENTRY = Path("services/worker/aether_worker/openhands_sandbox_entry.py")
_PYTHON = Path("environments/openhands/.venv/bin/python")
# cgroup limit is 786432 KiB. One sample peaked at 283888 KiB, and the process tree at 306484 KiB.
_ENTRY_RSS_BUDGET_KB = 700000
_ENTRY_TREE_BUDGET_KB = 750000
_PROMPT_TOKENS = 17
_COMPLETION_TOKENS = 9


# Stub protocols sign their one result line with the nonce the entry hands them, like coding_protocol.
_SIGN = "import os as _os\n_NONCE = open(_os.environ['AETHER_PROTOCOL_NONCE_FILE']).read().strip()\n"


def _call_names(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                found.append(func.id)
            elif isinstance(func, ast.Attribute):
                found.append(func.attr)
        found.extend(_call_names(child))
    return found


def _resident_kb(pid: int) -> int:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except OSError:
        return 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def _tree_resident_kb(pid: int) -> int:
    total = 0
    pending = [pid]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        total += _resident_kb(current)
        pending.extend(_children(current))
    return total


def _children(pid: int) -> list[int]:
    children = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        text = children.read_text(encoding="ascii")
    except OSError:
        return []
    return [int(item) for item in text.split()]


def test_entry_source_calls_the_agent_and_launch_keeps_other_images(monkeypatch) -> None:
    source = _ENTRY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    imported.extend(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module)
    attribute_calls = [
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    name_calls = [node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert "openhands.sdk" in imported
    assert "openhands.sdk.workspace.local" in imported
    assert "openhands.sdk.event" in imported
    assert "openhands.tools.file_editor" in imported
    assert "openhands.tools.terminal" in imported
    assert "Agent" in name_calls
    assert "Conversation" in name_calls
    assert "LLM" in name_calls
    assert "execute_command" in attribute_calls
    assert "mkdtemp" in attribute_calls
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    ordered = _call_names(main)
    assert ordered.index("_run_agent") < ordered.index("restore_grading_directory") < ordered.index("execute_command")
    assert "/tmp" not in source
    assert "_install_workspace_import_boundary" not in source
    assert not any(name == "importlib" or name.startswith("importlib.") for name in imported)
    assert launch_image("python:3.12-alpine", JOB_IMAGE) == "python:3.12-alpine"
    monkeypatch.setattr("aether_worker.openhands_image.coding_runtime_image", lambda: None)
    try:
        launch_image(JOB_IMAGE, JOB_IMAGE)
    except LookupError as exc:
        assert str(exc) == "image_unavailable"
    else:
        raise AssertionError("missing sandbox image was accepted")
    args = container_arguments(Path("."), JOB_IMAGE, objective="make value() return 42")
    assert args[-1] == "/opt/aether/openhands_sandbox_entry.py"
    assert "importlib" not in " ".join(args)
    assert str(CODING_MEMORY_BYTES) in args
    assert CODING_MEMORY_BYTES == 805306368


def test_agent_calls_the_model_socket_edits_the_file_and_strips_the_secret(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    fake = _SocketModel()
    fake.start()
    protocol = tmp_path / "protocol.py"
    protocol.write_text(
        _SIGN + "import json, os, sys, time\n"
        "time.sleep(0.3)\n"
        "body = {'nonce': _NONCE, "
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
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "OH_SECRET_KEY": "should-not-pass",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_OBJECTIVE": f"Edit {job / 'app.py'} so value() returns 42.",
            "AETHER_SANDBOX_PROTOCOL": str(protocol),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    proc = subprocess.Popen(
        [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak = 0
    tree_peak = 0
    deadline = time.monotonic() + 120
    while proc.poll() is None and time.monotonic() < deadline:
        peak = max(peak, _resident_kb(proc.pid))
        tree_peak = max(tree_peak, _tree_resident_kb(proc.pid))
        time.sleep(0.02)
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    finally:
        fake.close()
    line = next((item for item in reversed(stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert proc.returncode == 0 and line, stdout + stderr
    assert 0 < peak <= _ENTRY_RSS_BUDGET_KB, peak
    assert tree_peak <= _ENTRY_TREE_BUDGET_KB, tree_peak
    body = json.loads(line[len("RESULT ") :])
    assert body["executor"] == "openhands.sdk.Agent"
    assert body["passed"] == 1
    assert body["failed"] == 0
    assert body["oh_secret_visible"] is False
    assert body["ai_agent"] == "openhands"
    assert body["credential_present"] is True
    assert isinstance(body["network_errno"], int)
    assert body["edit_calls"] >= 1
    assert body["model_requests"] >= 2
    assert body["input_tokens"] == _PROMPT_TOKENS * len(fake.bodies)
    assert body["output_tokens"] == _COMPLETION_TOKENS * len(fake.bodies)
    assert "return 42" in body["patch"]
    assert "return 42" in (job / "app.py").read_text(encoding="utf-8")
    assert fake.authorizations
    assert set(fake.authorizations) == {"Bearer job-token-unit"}
    assert "file_editor" in fake.tool_names
    assert "terminal" in fake.tool_names
    assert fake.models
    assert set(fake.models) == {"coder"}
    assert peak > 200_000
    assert OPENHANDS_VERSION == "1.54.0"
    assert sys.version_info >= (3, 13)


def test_a_finish_call_leaves_the_candidate_file_unchanged(tmp_path: Path) -> None:
    fake = _SocketModel(finish_only=True)
    fake.start()
    protocol = tmp_path / "protocol.py"
    protocol.write_text(
        _SIGN + "import json, sys\n"
        "sys.stdout.write(json.dumps({'nonce': _NONCE, 'passed': 1, 'failed': 0, 'errors': 0, 'cases': []}) + '\\n')\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_OBJECTIVE": f"Edit {job / 'app.py'} so value() returns 42.",
            "AETHER_SANDBOX_PROTOCOL": str(protocol),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert completed.returncode == 0 and line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    assert body["edit_calls"] == 0
    assert body["model_requests"] >= 1
    assert body["patch"] == ""
    assert body["input_tokens"] == _PROMPT_TOKENS * len(fake.bodies)
    assert body["output_tokens"] == _COMPLETION_TOKENS * len(fake.bodies)
    assert "return 0" in (job / "app.py").read_text(encoding="utf-8")
    assert "return 42" not in (job / "app.py").read_text(encoding="utf-8")


def test_rewritten_tests_cannot_pass(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    fake = _SocketModel(rewrite_tests=True)
    fake.start()
    protocol = Path("services/worker/aether_worker/coding_protocol.py")
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    baseline = "import app\n\ndef test_value():\n    assert app.value() == 42\n"
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    (work / "test_app.py").write_text(baseline, encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_OBJECTIVE": f"Edit {job / 'app.py'} so value() returns 42.",
            "AETHER_SANDBOX_PROTOCOL": str(protocol.resolve()),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    graded = (job / "test_app.py").read_text(encoding="utf-8")
    assert completed.returncode != 0
    assert body["passed"] == 0
    assert body["failed"] >= 1
    assert body["edit_calls"] >= 1
    # A test edit is grading input, not a code change: it never enters the published patch.
    assert body["patch"] == ""
    assert graded == baseline
    assert "assert True" not in graded
    assert any(case["name"] == "test_app.test_value" and case["verdict"] == "FAIL" for case in body["cases"])


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


_ESCAPER = (
    "import os, sys, time\n"
    "pid_file, target = sys.argv[1], sys.argv[2]\n"
    "with open(pid_file, 'w') as handle:\n"
    "    handle.write(str(os.getpid()))\n"
    "body = 'import app\\n\\ndef test_value():\\n    assert True\\n'\n"
    "deadline = time.monotonic() + 60\n"
    "while time.monotonic() < deadline:\n"
    "    try:\n"
    "        with open(target, 'w') as handle:\n"
    "            handle.write(body)\n"
    "    except OSError:\n"
    "        pass\n"
    "    time.sleep(0.01)\n"
)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def test_background_process_cannot_rewrite_tests_after_restore(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    escaper = tmp_path / "escaper.py"
    escaper.write_text(_ESCAPER, encoding="utf-8")
    pid_file = tmp_path / "escaper.pid"
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    baseline = "import app\n\ndef test_value():\n    assert app.value() == 42\n"
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    (work / "test_app.py").write_text(baseline, encoding="utf-8")
    # The agent starts a writer that leaves its process group and session twice.
    command = (
        f"(setsid sh -c 'setsid {sys.executable} {escaper} {pid_file} {job / 'test_app.py'} "
        f">/dev/null 2>&1 </dev/null &' &) ; sleep 1"
    )
    fake = _SocketModel(terminal_command=command)
    fake.start()
    protocol = Path("services/worker/aether_worker/coding_protocol.py")
    env = os.environ.copy()
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_OBJECTIVE": f"Edit {job / 'app.py'} so value() returns 42.",
            "AETHER_SANDBOX_PROTOCOL": str(protocol.resolve()),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    pid = 0
    try:
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        assert pid_file.is_file(), completed.stdout + completed.stderr
        pid = int(pid_file.read_text(encoding="ascii"))
        left = _alive(pid)
        line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
        assert line, completed.stdout + completed.stderr
        body = json.loads(line[len("RESULT ") :])
        assert not left, f"agent process {pid} survived the agent run; passed={body['passed']}"
        assert completed.returncode != 0
        assert body["passed"] == 0
        assert body["failed"] >= 1
        assert (job / "test_app.py").read_text(encoding="utf-8") == baseline
    finally:
        fake.close()
        if pid and _alive(pid):
            os.kill(pid, 9)


_OBJECTIVE = (
    "Rename \"value\" to 'answer' in app.py\n"
    "then $(touch /tmp/pwned) && `id`; echo ${HOME} {target} {0} %s \\n 中文\n\n  trailing  "
)


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    assert isinstance(content, list), message
    return "".join(part["text"] for part in content if isinstance(part, dict) and part.get("type") == "text")


def _entry_env(tmp_path: Path, fake: _SocketModel, **extra: str) -> dict[str, str]:
    protocol = tmp_path / "protocol.py"
    protocol.write_text(
        _SIGN + "import json, sys\n"
        "sys.stdout.write(json.dumps({'nonce': _NONCE, 'passed': 1, 'failed': 0, 'errors': 0, 'cases': []}) + '\\n')\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "AETHER_OBJECTIVE"}
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(tmp_path / "job"),
            "AETHER_SANDBOX_PROTOCOL": str(protocol),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
            **extra,
        }
    )
    return env


def _run_entry(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_agent_receives_the_objective_verbatim_as_its_only_user_message(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    fake = _SocketModel(finish_only=True)
    fake.start()
    try:
        completed = _run_entry(tmp_path, _entry_env(tmp_path, fake, AETHER_OBJECTIVE=_OBJECTIVE))
    finally:
        fake.close()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert fake.bodies, completed.stdout + completed.stderr
    for raw in fake.bodies:
        messages = json.loads(raw)["messages"]
        users = [_message_text(item) for item in messages if item.get("role") == "user"]
        assert users == [_OBJECTIVE]
        systems = [_message_text(item) for item in messages if item.get("role") == "system"]
        assert len(systems) == 1
        assert _OBJECTIVE not in systems[0]
        assert "{target}" not in systems[0] and "pwned" not in systems[0]
        workdir = str(tmp_path / "job")
        assert workdir in systems[0]
        # The pytest tmp path can itself contain "42"; strip it before looking for demo wording.
        generic = systems[0].replace(workdir, "<job>")
        for demo in ("42", "returns 42", "return 0", "value()", "app.py", "str_replace", "file_editor str_replace"):
            assert demo not in generic, demo
    assert not Path("/tmp/pwned").exists()


def test_entry_fails_without_an_objective_and_never_calls_the_model(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    fake = _SocketModel()
    fake.start()
    try:
        missing = _run_entry(tmp_path / "a", _entry_env(_mkdir(tmp_path / "a"), fake))
        blank = _run_entry(tmp_path / "b", _entry_env(_mkdir(tmp_path / "b"), fake, AETHER_OBJECTIVE=" \n\t "))
    finally:
        fake.close()
    for completed in (missing, blank):
        line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
        assert line, completed.stdout + completed.stderr
        body = json.loads(line[len("RESULT ") :])
        assert completed.returncode == 2
        assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": "objective_missing"}]
        assert body["model_requests"] == 0
        assert body["patch"] == ""
    assert fake.bodies == []


def _mkdir(path: Path) -> Path:
    path.mkdir()
    return path


_LATE_WRITER = (
    "import os, signal, sys, time\n"
    "target, ready = sys.argv[1], sys.argv[2]\n"
    "def late(*_args):\n"
    "    with open(target, 'w') as handle:\n"
    "        handle.write('def value():\\n    return 99\\n')\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, late)\n"
    "with open(ready, 'w') as handle:\n"
    "    handle.write(str(os.getpid()))\n"
    "deadline = time.monotonic() + 60\n"
    "while time.monotonic() < deadline:\n"
    "    time.sleep(0.05)\n"
)


def _expected_patch(work: Path, job: Path) -> str:
    """The diff the grader actually sees: baseline workspace against the graded job copy."""
    chunks: list[str] = []
    names = sorted(
        {path.relative_to(root).as_posix() for root in (work, job) for path in root.rglob("*") if path.is_file()}
    )
    for name in names:
        if name.startswith(".aether-state/") or "__pycache__" in name:
            continue
        old = (work / name).read_text(encoding="utf-8") if (work / name).is_file() else ""
        new = (job / name).read_text(encoding="utf-8") if (job / name).is_file() else ""
        if old != new:
            chunks.extend(
                difflib.unified_diff(
                    old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{name}", tofile=f"b/{name}"
                )
            )
    return "".join(chunks)


def test_published_patch_matches_the_workspace_that_is_graded(tmp_path: Path) -> None:
    """A background process that edits app.py after the agent finishes, before it is stopped."""
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    writer = tmp_path / "late_writer.py"
    writer.write_text(_LATE_WRITER, encoding="utf-8")
    ready = tmp_path / "writer.pid"
    job = tmp_path / "job"
    command = (
        f"(setsid sh -c 'setsid {sys.executable} {writer} {job / 'app.py'} {ready} "
        f">/dev/null 2>&1 </dev/null &' &) ; sleep 1"
    )
    fake = _SocketModel(terminal_command=command)
    fake.start()
    protocol = tmp_path / "protocol.py"
    protocol.write_text(
        _SIGN + "import json, sys\n"
        "body = {'nonce': _NONCE, 'passed': 1, 'failed': 0, 'errors': 0, 'cases': [], 'graded': open('app.py').read()}\n"
        "sys.stdout.write(json.dumps(body) + '\\n')\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "AETHER_OBJECTIVE"}
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_OBJECTIVE": "Make value() return 42.",
            "AETHER_SANDBOX_PROTOCOL": str(protocol),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    pid = 0
    try:
        completed = _run_entry(tmp_path, env)
        assert ready.is_file(), completed.stdout + completed.stderr
        pid = int(ready.read_text(encoding="ascii"))
    finally:
        fake.close()
        if pid and _alive(pid):
            os.kill(pid, 9)
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    graded = (job / "app.py").read_text(encoding="utf-8")
    assert body["agent_completed"] is True
    assert body["graded"] == graded
    assert "return 99" in graded
    assert body["patch"] == _expected_patch(work, job)
    assert "+    return 99" in body["patch"]


_TAMPER = (
    "import sys\n"
    "job = sys.argv[1]\n"
    "open(job + '/oracle.py', 'w').write('def expected():\\n    return 0\\n')\n"
    "open(job + '/conftest.py', 'w').write('import oracle\\noracle.expected = lambda: 0\\n')\n"
    "open(job + '/sitecustomize.py', 'w').write('import oracle\\n')\n"
    "open(job + '/evil.pth', 'w').write('import oracle\\n')\n"
    "import os\n"
    "os.makedirs(job + '/__pycache__', exist_ok=True)\n"
    "open(job + '/__pycache__/planted.pyc', 'wb').write(b'x')\n"
    "os.makedirs(job + '/.pytest_cache', exist_ok=True)\n"
    "open(job + '/.pytest_cache/conftest.py', 'w').write('x')\n"
)


def test_tampered_support_files_are_restored_before_grading(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    tamper = tmp_path / "tamper.py"
    tamper.write_text(_TAMPER, encoding="utf-8")
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    oracle = "def expected():\n    return 42\n"
    conftest = "# baseline conftest\n"
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    (work / "oracle.py").write_text(oracle, encoding="utf-8")
    (work / "conftest.py").write_text(conftest, encoding="utf-8")
    (work / "test_app.py").write_text(
        "import app\nimport oracle\n\ndef test_value():\n    assert app.value() == oracle.expected()\n",
        encoding="utf-8",
    )
    fake = _SocketModel(terminal_command=f"{sys.executable} {tamper} {job}")
    fake.start()
    protocol = Path("services/worker/aether_worker/coding_protocol.py")
    env = os.environ.copy()
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_SANDBOX_PROTOCOL": str(protocol.resolve()),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
            "AETHER_OBJECTIVE": f"Edit {job / 'app.py'} so value() returns 42.",
        }
    )
    try:
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    # The agent only touched grading files and caches: none of that is a code change, so the
    # published patch is empty (the job still fails on the added startup hooks below).
    assert body["patch"] == ""
    assert {"name": "runner", "verdict": "FAIL", "detail": "grading_startup_hook"} in body["cases"]
    assert (job / "oracle.py").read_text(encoding="utf-8") == oracle
    assert (job / "conftest.py").read_text(encoding="utf-8") == conftest
    assert not (job / "sitecustomize.py").exists()
    assert not (job / "evil.pth").exists()
    # The protocol may write fresh bytecode; the planted file must be gone.
    assert not (job / "__pycache__" / "planted.pyc").exists()
    assert not (job / ".pytest_cache").exists()
    assert body["passed"] == 0
    assert completed.returncode != 0


_EDIT_SOURCES = (
    "import sys\n"
    "job = sys.argv[1]\n"
    "open(job + '/helper.py', 'w').write('import newmod\\n\\ndef value():\\n    return newmod.ANSWER\\n')\n"
    "open(job + '/newmod.py', 'w').write('ANSWER = 42\\n')\n"
    "open(job + '/oracle.py', 'w').write('def expected():\\n    return -1\\n')\n"
    "open(job + '/conftest.py', 'w').write('raise SystemExit(1)\\n')\n"
)


def _run_tamper_job(
    tmp_path: Path, script: str, files: dict[str, str], *, revoke_mode: str = "ok"
) -> tuple[subprocess.CompletedProcess[str], dict]:
    tamper = tmp_path / "tamper.py"
    tamper.write_text(script, encoding="utf-8")
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    for name, text in files.items():
        (work / name).write_text(text, encoding="utf-8")
    fake = _SocketModel(terminal_command=f"{sys.executable} {tamper} {job}")
    fake.revoke_mode = revoke_mode
    fake.start()
    protocol = Path("services/worker/aether_worker/coding_protocol.py")
    env = os.environ.copy()
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_SANDBOX_PROTOCOL": str(protocol.resolve()),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
            "AETHER_OBJECTIVE": f"Edit {job / 'helper.py'} so value() returns 42.",
        }
    )
    try:
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    return completed, json.loads(line[len("RESULT ") :])


def test_edited_and_new_source_files_are_graded_while_support_files_are_restored(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    oracle = "def expected():\n    return 42\n"
    conftest = "# baseline conftest\n"
    completed, body = _run_tamper_job(
        tmp_path,
        _EDIT_SOURCES,
        {
            "app.py": "def value():\n    return 0\n",
            "helper.py": "def value():\n    return 0\n",
            "oracle.py": oracle,
            "conftest.py": conftest,
            "test_helper.py": "import helper\nimport oracle\n\ndef test_value():\n    assert helper.value() == oracle.expected()\n",
        },
    )
    job = tmp_path / "job"
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert body["passed"] == 1 and body["failed"] == 0
    assert (job / "helper.py").read_text(encoding="utf-8").startswith("import newmod")
    assert (job / "newmod.py").read_text(encoding="utf-8") == "ANSWER = 42\n"
    assert (job / "oracle.py").read_text(encoding="utf-8") == oracle
    assert (job / "conftest.py").read_text(encoding="utf-8") == conftest
    assert "+++ b/helper.py" in body["patch"]
    assert "+++ b/newmod.py" in body["patch"]


def test_added_startup_hook_is_deleted_and_fails_the_job(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    script = (
        "import sys\n"
        "job = sys.argv[1]\n"
        "open(job + '/app.py', 'w').write('def value():\\n    return 42\\n')\n"
        "open(job + '/usercustomize.py', 'w').write('import os\\n')\n"
        "open(job + '/hook.pth', 'w').write('import os\\n')\n"
    )
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            "app.py": "def value():\n    return 0\n",
            "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    job = tmp_path / "job"
    assert completed.returncode == 1
    assert body["passed"] == 0
    assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": "grading_startup_hook"}]
    assert not (job / "usercustomize.py").exists()
    assert not (job / "hook.pth").exists()
    assert "+++ b/app.py" in body["patch"]


def test_added_hidden_pytest_config_cannot_redirect_grading(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    script = (
        "import sys\n"
        "job = sys.argv[1]\n"
        "for name in ('.pytest.ini', 'pytest.toml', '.pytest.toml'):\n"
        "    open(job + '/' + name, 'w').write('[pytest]\\npython_files = my_tests.py\\n')\n"
        "open(job + '/my_tests.py', 'w').write('def test_ok():\\n    assert True\\n')\n"
    )
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            "app.py": "def value():\n    return 0\n",
            "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    job = tmp_path / "job"
    for name in (".pytest.ini", "pytest.toml", ".pytest.toml"):
        assert not (job / name).exists(), name
    assert completed.returncode == 1
    assert body["passed"] == 0
    assert [case["name"] for case in body["cases"]] == ["test_app.test_value"]
    assert body["cases"][0]["verdict"] == "FAIL"


@pytest.mark.parametrize("planted", ["pytest.py", "_pytest/__init__.py", "json.py"])
def test_added_shadow_module_is_deleted_and_fails_the_job(tmp_path: Path, planted: str) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    script = (
        "import os, sys\n"
        "job = sys.argv[1]\n"
        "open(job + '/app.py', 'w').write('def value():\\n    return 42\\n')\n"
        f"target = os.path.join(job, {planted!r})\n"
        "os.makedirs(os.path.dirname(target), exist_ok=True)\n"
        "open(target, 'w').write('raise SystemExit(0)\\n')\n"
    )
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            "app.py": "def value():\n    return 0\n",
            "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    job = tmp_path / "job"
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert body["passed"] == 0
    assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": "grading_shadow_module"}]
    assert not (job / planted.split("/")[0]).exists()
    assert "+++ b/app.py" in body["patch"]


@pytest.mark.parametrize("added", ["utils.py", "config.py"])
def test_added_generic_module_is_kept_and_graded(tmp_path: Path, added: str) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    # The sandbox venv stands in for the image; it must not ship a package with this name.
    probe = subprocess.run(
        [str(_PYTHON.absolute()), "-I", "-c", "from importlib import metadata; print(sorted(metadata.packages_distributions()))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert repr(added[:-3]) not in probe.stdout, f"{added[:-3]} is installed in the sandbox venv"
    script = (
        "import sys\n"
        "job = sys.argv[1]\n"
        f"open(job + '/{added}', 'w').write('ANSWER = 42\\n')\n"
        f"open(job + '/app.py', 'w').write('from {added[:-3]} import ANSWER\\n\\ndef value():\\n    return ANSWER\\n')\n"
    )
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            "app.py": "def value():\n    return 0\n",
            "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    job = tmp_path / "job"
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert body["passed"] == 1 and body["failed"] == 0
    assert (job / added).read_text(encoding="utf-8") == "ANSWER = 42\n"
    assert f"+++ b/{added}" in body["patch"]


def test_only_grading_and_cache_edits_leave_an_empty_patch_and_no_change(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    job = tmp_path / "job"
    command = (
        f"cd {job} && printf 'import pytest\\n' > conftest.py && "
        "printf 'import app\\n\\ndef test_value():\\n    assert True\\n' > test_app.py && "
        "mkdir -p .pytest_cache/v/cache tests && printf '{}' > .pytest_cache/v/cache/lastfailed && "
        "printf '' > tests/__init__.py && printf '[pytest]\\n' > pytest.ini && "
        f"touch {tmp_path / 'command-ran'}"
    )
    fake = _SocketModel(terminal_command=command)
    fake.start()
    env = _entry_env(tmp_path, fake, AETHER_OBJECTIVE="Make value() return 42.")
    try:
        completed = _run_entry(tmp_path, env)
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    assert (tmp_path / "command-ran").is_file(), completed.stdout + completed.stderr
    # The agent's grading-only files were removed or restored before grading.
    assert not (job / "conftest.py").exists() and not (job / ".pytest_cache").exists()
    assert not (job / "pytest.ini").exists() and not (job / "tests" / "__init__.py").exists()
    assert body["agent_completed"] is True
    assert body["patch"] == ""
    assert ".pytest_cache" not in body["patch"]
    from aether_worker.coding_jobs import _coding_terminal

    result = {**body, "model_requests": 2, "credential_present": False, "network_errno": 101}
    sandbox = {"exit_code": 0, "result": result}
    assert _coding_terminal(sandbox, upstream_error=None, measured=(8, 4), usage_missing=False) == (
        "FAILED",
        "agent_no_change",
    )


def test_other_source_edits_and_new_source_files_are_published_as_graded(tmp_path: Path) -> None:
    """A change to a non-grading file besides app.py, and a new source file, are both in the patch."""
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    job = tmp_path / "job"
    command = (
        f"cd {job} && printf 'FACTOR = 6\\n' > util.py && "
        "printf 'def helper():\\n    return 7\\n' > helper.py && "
        "printf 'import pytest\\n' > conftest.py && "
        "mkdir -p .pytest_cache && printf '{}' > .pytest_cache/lastfailed"
    )
    fake = _SocketModel(terminal_command=command)
    fake.start()
    env = _entry_env(tmp_path, fake, AETHER_OBJECTIVE="Make value() return 42.")
    work = tmp_path / "work"
    (work / "util.py").write_text("FACTOR = 1\n", encoding="utf-8")
    try:
        completed = _run_entry(tmp_path, env)
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    assert body["agent_completed"] is True
    graded_util = (job / "util.py").read_text(encoding="utf-8")
    graded_helper = (job / "helper.py").read_text(encoding="utf-8")
    assert graded_util == "FACTOR = 6\n"
    assert graded_helper == "def helper():\n    return 7\n"
    expected = "".join(
        [
            *difflib.unified_diff([], graded_helper.splitlines(keepends=True), fromfile="a/helper.py", tofile="b/helper.py"),
            *difflib.unified_diff(
                ["FACTOR = 1\n"], graded_util.splitlines(keepends=True), fromfile="a/util.py", tofile="b/util.py"
            ),
        ]
    )
    # Exactly the graded source changes: no conftest.py, no cache.
    assert body["patch"] == expected


_GRADING_REWRITER = (
    "import os, signal, sys, time\n"
    "job, ready = sys.argv[1], sys.argv[2]\n"
    "def write(name, text):\n"
    "    with open(os.path.join(job, name), 'w') as handle:\n"
    "        handle.write(text)\n"
    "write('test_app.py', 'def test_value():\\n    assert True\\n')\n"
    "write('conftest.py', 'import app\\napp.value = lambda: 42\\n')\n"
    "write('helper.py', 'def bonus():\\n    return 1\\n')\n"
    "def late(*_args):\n"
    "    write('test_app.py', 'def test_value():\\n    pass\\n')\n"
    "    write('conftest.py', 'raise SystemExit(0)\\n')\n"
    "    write('app.py', 'def value():\\n    return 42\\n')\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, late)\n"
    "write(ready, str(os.getpid()))\n"
    "deadline = time.monotonic() + 60\n"
    "while time.monotonic() < deadline:\n"
    "    time.sleep(0.05)\n"
)


def test_published_patch_excludes_grading_rewrites_even_from_late_background_writers(tmp_path: Path) -> None:
    """The agent and a background writer it leaves behind rewrite tests, conftest and sources."""
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    writer = tmp_path / "rewriter.py"
    writer.write_text(_GRADING_REWRITER, encoding="utf-8")
    ready = tmp_path / "writer.pid"
    job = tmp_path / "job"
    command = (
        f"(setsid sh -c 'setsid {sys.executable} {writer} {job} {ready} "
        f">/dev/null 2>&1 </dev/null &' &) ; sleep 1"
    )
    fake = _SocketModel(terminal_command=command)
    fake.start()
    protocol = Path("services/worker/aether_worker/coding_protocol.py")
    work = tmp_path / "work"
    work.mkdir()
    baseline_test = "import app\n\ndef test_value():\n    assert app.value() == 42\n"
    baseline_conftest = "# baseline conftest\n"
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    (work / "test_app.py").write_text(baseline_test, encoding="utf-8")
    (work / "conftest.py").write_text(baseline_conftest, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "AETHER_OBJECTIVE"}
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_OBJECTIVE": "Make value() return 42.",
            "AETHER_SANDBOX_PROTOCOL": str(protocol.resolve()),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    pid = 0
    try:
        completed = _run_entry(tmp_path, env)
        assert ready.is_file(), completed.stdout + completed.stderr
        pid = int(ready.read_text(encoding="ascii"))
    finally:
        fake.close()
        if pid and _alive(pid):
            os.kill(pid, 9)
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    patch = body["patch"]
    # Graded workspace: trusted tests restored, the late source edit kept.
    assert (job / "test_app.py").read_text(encoding="utf-8") == baseline_test
    assert (job / "conftest.py").read_text(encoding="utf-8") == baseline_conftest
    assert (job / "app.py").read_text(encoding="utf-8") == "def value():\n    return 42\n"
    # Published patch: source changes only, including the late one, and no grading-file change.
    assert "+++ b/app.py" in patch and "+    return 42" in patch
    assert "+++ b/helper.py" in patch
    assert "test_app.py" not in patch
    assert "conftest.py" not in patch
    assert "assert True" not in patch and "SystemExit" not in patch
    # It is exactly the baseline-to-graded diff (restored grading files contribute nothing).
    assert patch == _expected_patch(work, job)
    assert completed.returncode == 0, completed.stdout + completed.stderr


class _SocketModel:
    def __init__(
        self,
        *,
        finish_only: bool = False,
        rewrite_tests: bool = False,
        terminal_command: str = "",
    ) -> None:
        self.finish_only = finish_only
        self.terminal_command = terminal_command
        self.rewrite_tests = rewrite_tests
        self.path = Path(os.environ.get("TMPDIR", "/tmp")) / f"aether-model-test-{os.getpid()}.sock"
        self.authorizations: list[str] = []
        self.bodies: list[bytes] = []
        self.tool_names: set[str] = set()
        self.models: list[str] = []
        self.revoked = False
        # How the fake answers the entry's revoke: ok (200, socket removed), status (500), drop (closes
        # without answering), hang (answers after the entry's timeout), keep (200, socket left in place).
        self.revoke_mode = "ok"
        self._calls = 0
        self._server = _UnixServer(str(self.path), _Handler)
        self._server.model = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self.path.unlink(missing_ok=True)

    def reply(self, raw: bytes, authorization: str) -> dict:
        self.authorizations.append(authorization)
        self.bodies.append(raw)
        self._calls += 1
        loaded = json.loads(raw)
        model = loaded.get("model")
        if isinstance(model, str):
            self.models.append(model)
        for tool in loaded.get("tools") or []:
            function = tool.get("function") if isinstance(tool, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str):
                self.tool_names.add(name)
        text = raw.decode("utf-8", "replace")
        if self._calls == 1 and self.terminal_command:
            arguments = json.dumps({"command": self.terminal_command})
            name = "terminal"
            call_id = "call_terminal"
        elif self._calls == 1 and self.rewrite_tests:
            target = str(Path(_app_path(text)).with_name("test_app.py"))
            arguments = json.dumps(
                {"command": "str_replace", "path": target, "old_str": "assert app.value() == 42", "new_str": "assert True"}
            )
            name = "file_editor"
            call_id = "call_edit"
        elif self._calls == 1 and not self.finish_only:
            target = _app_path(text)
            arguments = json.dumps({"command": "str_replace", "path": target, "old_str": "return 0", "new_str": "return 42"})
            name = "file_editor"
            call_id = "call_edit"
        else:
            arguments = json.dumps({"message": "done"})
            name = "finish"
            call_id = "call_finish"
        return _completion(name, arguments, call_id)


def _app_path(text: str) -> str:
    marker = "/app.py"
    start = text.rfind(marker)
    assert start > 0, text[:500]
    end = start + len(marker)
    begin = max(text.rfind(" ", 0, start), text.rfind('"', 0, start)) + 1
    return text[begin:end]


def _completion(name: str, arguments: str, call_id: str) -> dict:
    return {
        "id": call_id,
        "object": "chat.completion",
        "created": 0,
        "model": "coder",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}],
                },
            }
        ],
        "usage": {
            "prompt_tokens": _PROMPT_TOKENS,
            "completion_tokens": _COMPLETION_TOKENS,
            "total_tokens": _PROMPT_TOKENS + _COMPLETION_TOKENS,
        },
    }


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    model: _SocketModel

    def server_bind(self) -> None:
        if Path(self.server_address).exists():
            Path(self.server_address).unlink()
        super().server_bind()


class _Handler(socketserver.StreamRequestHandler):
    server: _UnixServer

    def handle(self) -> None:
        parsed = _read_http(self.rfile)
        if parsed is None:
            return
        _method, path, headers, raw = parsed
        if path == "/aether/revoke":
            # Like the host proxy: the job token ends the channel and the socket goes away.
            model = self.server.model
            mode = model.revoke_mode
            if mode == "drop":
                return
            if mode == "hang":
                time.sleep(8)
            if mode == "status":
                self.wfile.write(b"HTTP/1.1 500 Internal Server Error\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}")
                return
            model.revoked = headers.get("authorization", "") == "Bearer job-token-unit"
            self.wfile.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}")
            if model.revoked and mode != "keep":
                model.path.unlink(missing_ok=True)
            return
        payload = self.server.model.reply(raw, headers.get("authorization", ""))
        encoded = json.dumps(payload).encode()
        head = (
            "HTTP/1.1 200 OK\r\n"
            "content-type: application/json\r\n"
            f"content-length: {len(encoded)}\r\n"
            "connection: close\r\n"
            "\r\n"
        )
        self.wfile.write(head.encode("ascii") + encoded)


def _read_http(reader) -> tuple[str, str, dict[str, str], bytes] | None:
    start = reader.readline(8192)
    if not start:
        return None
    parts = start.decode("iso-8859-1", "replace").strip().split(" ")
    if len(parts) < 2:
        return None
    headers: dict[str, str] = {}
    for _ in range(100):
        line = reader.readline(8192)
        if line in {b"\r\n", b"\n", b""}:
            break
        decoded = line.decode("iso-8859-1", "replace")
        if ":" not in decoded:
            return None
        name, value = decoded.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    else:
        return None
    length = int(headers.get("content-length") or "0")
    raw = reader.read(length)
    return parts[0].upper(), parts[1], headers, raw


def _sandbox_context(root: Path) -> None:
    from aether_worker.openhands_image import DOCKERFILE

    source = Path(__file__).resolve().parents[2]
    for name in (
        DOCKERFILE,
        "environments/openhands/pyproject.toml",
        "environments/openhands/uv.lock",
        "services/worker/aether_worker/coding_protocol.py",
        "services/worker/aether_worker/grading_files.py",
        "services/worker/aether_worker/openhands_sandbox_entry.py",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())
    (root / "environments/openhands/wheels").mkdir(parents=True, exist_ok=True)
    (root / "environments/openhands/wheels/local.whl").write_bytes(b"wheel")


def test_sandbox_tag_hashes_the_build_context_and_lock_files(tmp_path: Path) -> None:
    from aether_worker.openhands_image import DOCKERFILE, sandbox_tag

    root = tmp_path / "repo"
    _sandbox_context(root)
    first = sandbox_tag(root)
    assert first.startswith(f"aether-openhands-sandbox:{OPENHANDS_VERSION}-")
    assert sandbox_tag(root) == first
    seen = {first}
    for name in (
        DOCKERFILE,
        "environments/openhands/uv.lock",
        "environments/openhands/pyproject.toml",
        "environments/openhands/wheels/local.whl",
        "services/worker/aether_worker/coding_protocol.py",
        "services/worker/aether_worker/grading_files.py",
        "services/worker/aether_worker/openhands_sandbox_entry.py",
    ):
        path = root / name
        original = path.read_bytes()
        path.write_bytes(original + b"\n# changed\n")
        changed = sandbox_tag(root)
        assert changed not in seen, name
        seen.add(changed)
        path.write_bytes(original)
        assert sandbox_tag(root) == first


_FORGE_RESULT = (
    "import sys\n"
    "job = sys.argv[1]\n"
    "open(job + '/app.py', 'w').write(\n"
    "    'import json, os, sys\\n'\n"
    "    'line = json.dumps({\"passed\": 1, \"failed\": 0, \"errors\": 0, \"cases\": '\n"
    "    '[{\"name\": \"test_app.test_value\", \"verdict\": \"PASS\", \"detail\": \"\"}]})\\n'\n"
    "    'sys.stdout.write(line + \"\\\\n\")\\n'\n"
    "    'sys.stdout.flush()\\n'\n"
    "    'os._exit(0)\\n'\n"
    ")\n"
)


def test_candidate_output_cannot_forge_the_protocol_result(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    completed, body = _run_tamper_job(
        tmp_path,
        _FORGE_RESULT,
        {
            "app.py": "def value():\n    return 0\n",
            "oracle.py": "def expected():\n    return 42\n",
            "test_app.py": "import app\nimport oracle\n\ndef test_value():\n    assert app.value() == oracle.expected()\n",
        },
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert body["passed"] == 0 and body["failed"] >= 1, body
    assert "nonce" not in body


def test_a_protocol_that_reports_nothing_is_a_grading_timeout_not_a_violation(tmp_path: Path) -> None:
    """No result line at all (the grader hung, was killed or crashed) is grading_timeout; only a forged,
    unsigned or duplicated result line is the grading_unverified violation."""
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    fake = _SocketModel(finish_only=True)
    fake.start()
    try:
        env = _entry_env(tmp_path, fake, AETHER_OBJECTIVE=_OBJECTIVE)
        Path(env["AETHER_SANDBOX_PROTOCOL"]).write_text("import sys\nsys.stderr.write('stuck\\n')\nraise SystemExit(1)\n", encoding="utf-8")
        completed = _run_entry(tmp_path, env)
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout + completed.stderr
    body = json.loads(line[len("RESULT ") :])
    assert completed.returncode == 1, completed.stdout + completed.stderr
    details = [case["detail"] for case in body["cases"] if case["name"] == "runner"]
    assert details == ["grading_timeout"], body


@pytest.mark.parametrize("baseline_name", ["json.py", "decimal.py"])
def test_a_rewritten_baseline_module_named_like_stdlib_is_restored_and_fails_the_job(
    tmp_path: Path, baseline_name: str
) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    script = (
        "import os, sys\n"
        "job = sys.argv[1]\n"
        "open(job + '/app.py', 'w').write('def value():\\n    return 42\\n')\n"
        f"open(os.path.join(job, {baseline_name!r}), 'w').write('raise SystemExit(0)\\n')\n"
    )
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            "app.py": "def value():\n    return 0\n",
            baseline_name: "VALUE = 1\n",
            "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    job = tmp_path / "job"
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": "grading_shadow_module"}]
    assert (job / baseline_name).read_text(encoding="utf-8") == "VALUE = 1\n"


def test_an_untouched_baseline_module_named_like_stdlib_does_not_fail_the_job(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    script = "import sys\nopen(sys.argv[1] + '/app.py', 'w').write('def value():\\n    return 42\\n')\n"
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            "app.py": "def value():\n    return 0\n",
            "json.py": "VALUE = 1\n",
            "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert body["cases"] == [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}]


def test_candidate_code_cannot_use_the_model_channel_during_grading(tmp_path: Path) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    # At agent time the terminal has the model env; the agent bakes the socket path into app.py.
    script = (
        "import os, sys\n"
        "job = sys.argv[1]\n"
        "socket_path = os.environ.get('AETHER_MODEL_SOCKET', '')\n"
        "open(job + '/app.py', 'w').write(\n"
        "    'import os, socket\\n'\n"
        "    f'SOCKET = {socket_path!r}\\n'\n"
        "    'def value():\\n    return 42\\n'\n"
        "    'def channel():\\n'\n"
        "    '    seen = sorted(n for n in (\"AETHER_JOB_TOKEN\", \"AETHER_MODEL_SOCKET\", \"AETHER_MODEL_ALIAS\") if n in os.environ)\\n'\n"
        "    '    try:\\n'\n"
        "    '        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:\\n'\n"
        "    '            s.connect(SOCKET)\\n'\n"
        "    '        reachable = True\\n'\n"
        "    '    except OSError:\\n'\n"
        "    '        reachable = False\\n'\n"
        "    '    return [seen, bool(SOCKET), reachable]\\n'\n"
        ")\n"
    )
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            "app.py": "def value():\n    return 0\n",
            "test_app.py": (
                "import app\n\n"
                "def test_value():\n    assert app.value() == 42\n\n"
                "def test_no_model_channel():\n    assert app.channel() == [[], True, False]\n"
            ),
        },
    )
    assert body["cases"] == [
        {"name": "test_app.test_no_model_channel", "verdict": "PASS", "detail": ""},
        {"name": "test_app.test_value", "verdict": "PASS", "detail": ""},
    ], completed.stdout + completed.stderr


@pytest.mark.parametrize("mode", ["status", "drop", "hang", "keep"])
def test_grading_does_not_run_unless_the_model_channel_is_confirmed_closed(tmp_path: Path, mode: str) -> None:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    marker = tmp_path / "candidate-ran"
    script = "import sys\nopen(sys.argv[1] + '/app.py', 'a').write('\\ndef value():\\n    return 42\\n')\n"
    completed, body = _run_tamper_job(
        tmp_path,
        script,
        {
            # Any import of the candidate module during grading leaves the marker behind.
            "app.py": f"open({str(marker)!r}, 'w').write('ran')\n\ndef value():\n    return 0\n",
            "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
        revoke_mode=mode,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": "model_channel_open"}], body
    assert not marker.exists(), "candidate code ran although the model channel was still open"
    assert body["agent_completed"] is True
    assert "app.py" in body["patch"]
