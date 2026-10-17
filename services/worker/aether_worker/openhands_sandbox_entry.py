"""Sandbox entry. OpenHands Agent edits the workspace, then LocalWorkspace runs the protocol."""

from __future__ import annotations

import ctypes
import difflib
import hashlib
import json
import os
import signal
import socket
import socketserver
import sys
import tempfile
import threading
import time
from collections.abc import Collection
from pathlib import Path

os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import openhands.tools.file_editor as file_editor_tool
import openhands.tools.terminal as terminal_tool
from openhands.sdk import LLM, Agent, Conversation, ConversationExecutionStatus, Tool
from openhands.sdk.event import ActionEvent
from openhands.sdk.workspace.local import LocalWorkspace

# grading_files.py ships next to this script (repo and image); `-I` drops the script dir from sys.path.
# Appended, then removed, so it cannot shadow any other module.
_HERE = str(Path(__file__).resolve().parent)
sys.path.append(_HERE)
try:
    import grading_files
finally:
    sys.path.remove(_HERE)

WORK = Path(os.environ.get("AETHER_SANDBOX_WORK", "/work"))
PROTOCOL = Path(os.environ.get("AETHER_SANDBOX_PROTOCOL", "/opt/aether/coding_protocol.py"))
EXECUTOR = "openhands.sdk.Agent"
_STATE_DIR = ".aether-state"
_OBJECTIVE = "AETHER_OBJECTIVE"
_SKIP = {".git", "__pycache__", _STATE_DIR}
# Same value as coding_sandbox.MAX_PATCH_BYTES: the RESULT line must fit the worker's stdout window.
MAX_PATCH_BYTES = 2_097_152


class _Pump(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    socket_path = ""


class _PumpHandler(socketserver.BaseRequestHandler):
    server: _Pump

    def handle(self) -> None:
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            remote.settimeout(30)
            remote.connect(self.server.socket_path)
            self.request.settimeout(30)
            _bridge(self.request, remote)
        except OSError:
            return
        finally:
            remote.close()


def _shutdown_write(target: socket.socket) -> None:
    try:
        target.shutdown(socket.SHUT_WR)
    except OSError:
        return


def _bridge(left: socket.socket, right: socket.socket) -> None:
    def copy(source: socket.socket, target: socket.socket) -> None:
        try:
            while True:
                chunk = source.recv(65536)
                if not chunk:
                    break
                target.sendall(chunk)
        except OSError:
            return
        finally:
            _shutdown_write(target)

    forward = threading.Thread(target=copy, args=(left, right), daemon=True)
    backward = threading.Thread(target=copy, args=(right, left), daemon=True)
    forward.start()
    backward.start()
    forward.join(40)
    backward.join(40)


def _start_pump(socket_path: str) -> _Pump:
    server = _Pump(("127.0.0.1", 0), _PumpHandler)
    server.socket_path = socket_path
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def copy_tree(src: Path, dst: Path) -> None:
    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        if any(part in {".git", "__pycache__"} for part in relative.parts):
            continue
        if path.is_symlink():
            raise RuntimeError("symlink")
        target = dst.joinpath(*relative.parts)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise RuntimeError("special")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


def network_errno() -> int | None:
    sock = socket.socket()
    try:
        sock.settimeout(1)
        sock.connect(("1.1.1.1", 443))
    except OSError as exc:
        return exc.errno
    else:
        return 0
    finally:
        sock.close()


def credential_present() -> bool:
    markers = ("LIVE_MODEL", "ADMIN_DATABASE", "PASSWORD", "SECRET")
    return any(any(part in name for part in markers) for name in os.environ)


def emit(body: dict[str, object], code: int) -> None:
    patch = body.get("patch")
    if isinstance(patch, str) and len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        body["patch"] = ""
        body["patch_omitted"] = "too_large"
    sys.stdout.write("RESULT " + json.dumps(body, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    raise SystemExit(code)


def _failure(detail: str) -> dict[str, object]:
    return {
        "passed": 0,
        "failed": 1,
        "errors": 1,
        "cases": [{"name": "runner", "verdict": "FAIL", "detail": detail}],
        "elapsed_seconds": 0,
        "network_errno": network_errno(),
        "credential_present": credential_present(),
        "executor": EXECUTOR,
        "input_tokens": 0,
        "output_tokens": 0,
        "edit_calls": 0,
        "model_requests": 0,
        "patch": "",
        "agent_completed": False,
    }


def _protocol_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{") and line.endswith("}"):
            body = json.loads(line)
            if isinstance(body, dict):
                return body
    raise json.JSONDecodeError("protocol", stdout, 0)


def job_directory() -> Path:
    """Use the caller path, or a private directory from tempfile. A fixed public path is not created."""
    configured = os.environ.get("AETHER_SANDBOX_JOB")
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="aether-job-"))


def _snapshot(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _SKIP for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found[relative.as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return found


def _lines(value: str) -> list[str]:
    if not value:
        return []
    lines = value.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def _patch(before: dict[str, str], root: Path) -> str:
    """The agent's source change. Grading files and caches are grading input, never part of it."""
    after = _snapshot(root)
    chunks: list[str] = []
    for name in sorted(set(before) | set(after)):
        if grading_files.is_grading_path(name) or grading_files.is_cache_path(name):
            continue
        old = before.get(name, "")
        new = after.get(name, "")
        if old == new:
            continue
        chunks.extend(difflib.unified_diff(_lines(old), _lines(new), fromfile=f"a/{name}", tofile=f"b/{name}"))
    text = "".join(chunks)
    if not text.startswith("--- "):
        return ""
    return text


def _blank_report() -> dict[str, object]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "edit_calls": 0,
        "model_requests": 0,
        "patch": "",
        "agent_completed": False,
    }


def _collect(agent: Agent, conversation: Conversation) -> dict[str, object]:
    usage = agent.llm.metrics.accumulated_token_usage
    incoming = int(usage.prompt_tokens) if usage is not None else 0
    outgoing = int(usage.completion_tokens) if usage is not None else 0
    edits = 0
    for event in conversation.state.events:
        if isinstance(event, ActionEvent) and event.tool_name == file_editor_tool.FileEditorTool.name:
            edits += 1
    return {
        "input_tokens": incoming,
        "output_tokens": outgoing,
        "edit_calls": edits,
        "model_requests": len(agent.llm.metrics.token_usages),
        # Filled in by main after stop_agent_processes, so late writers are included.
        "patch": "",
        "agent_completed": True,
    }


class ObjectiveMissing(RuntimeError):
    pass


def job_objective() -> str:
    """The job objective from the environment, unchanged. A missing or blank one is a runner failure."""
    objective = os.environ.get(_OBJECTIVE, "")
    if not objective.strip():
        raise ObjectiveMissing("objective_missing")
    return objective


def system_prompt(job: Path) -> str:
    """Generic coding instructions. The objective is not part of it; it arrives as the user message."""
    return (
        "You are a coding agent. The user message is the task. "
        f"The repository is the directory {job}; work only inside it and use absolute paths under it. "
        "Read the relevant files, then make the change with file_editor or the terminal. "
        "Tests are restored and run after you finish, so do not edit test files to make them pass. "
        "There is no network access. Call finish when the change is complete."
    )


def _run_agent(job: Path, objective: str) -> dict[str, object]:
    report = _blank_report()
    socket_path = os.environ.get("AETHER_MODEL_SOCKET", "")
    token = os.environ.get("AETHER_JOB_TOKEN", "")
    if not socket_path or not token:
        return report
    server: _Pump | None = None
    try:
        server = _start_pump(socket_path)
        port = int(server.server_address[1])
        alias = os.environ.get("AETHER_MODEL_ALIAS", "openai/coder")
        llm = LLM(
            model=alias,
            api_key=token,
            base_url=f"http://127.0.0.1:{port}/v1",
            usage_id="coding",
            num_retries=0,
            timeout=30,
            native_tool_calling=True,
            stream=False,
        )
        agent = Agent(
            llm=llm,
            tools=[
                Tool(name=file_editor_tool.FileEditorTool.name),
                Tool(name=terminal_tool.TerminalTool.name, params={"terminal_type": "subprocess"}),
            ],
            condenser=None,
            system_prompt=system_prompt(job),
        )
        state = job / _STATE_DIR
        state.mkdir(parents=True, exist_ok=True)
        conversation = Conversation(
            agent=agent,
            workspace=job,
            max_iteration_per_run=4,
            stuck_detection=False,
            visualizer=None,
            persistence_dir=state,
        )
        conversation.send_message(objective)
        conversation.run()
        if conversation.state.execution_status == ConversationExecutionStatus.FINISHED:
            report = _collect(agent, conversation)
    except Exception as exc:
        sys.stderr.write(f"agent_failed {type(exc).__name__}\n")
        return report
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    return report


_PR_SET_CHILD_SUBREAPER = 36
_REAP_GRACE_SECONDS = 2.0
_REAP_DEADLINE_SECONDS = 10.0


def become_subreaper() -> None:
    """Orphans of the agent, including ones that call setsid twice, are reparented to this process."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise RuntimeError("subreaper_unavailable")


def _process_table() -> dict[int, tuple[int, int, int, str]]:
    """pid -> (ppid, pgid, sid, state), read from /proc."""
    table: dict[int, tuple[int, int, int, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        fields = raw.rsplit(")", 1)[-1].split()
        if len(fields) < 5:
            continue
        table[int(entry.name)] = (int(fields[1]), int(fields[2]), int(fields[3]), fields[0])
    return table


def _descendants(root: int) -> set[int]:
    """Live descendants of root. With the subreaper set, escaped orphans stay in this tree."""
    table = _process_table()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _pgid, _sid, _state) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    pending = list(children.get(root, []))
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, []))
    # Process groups and sessions started inside the tree are swept too.
    groups = {table[pid][1] for pid in found} | {table[pid][2] for pid in found}
    groups.discard(os.getpgid(0))
    groups.discard(os.getsid(0))
    for pid, (_ppid, pgid, sid, _state) in table.items():
        if pid != root and (pgid in groups or sid in groups):
            found.add(pid)
    return {pid for pid in found if pid in table and table[pid][3] != "Z"}


def _reap_zombies() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _signal_all(pids: set[int], sig: int) -> None:
    table = _process_table()
    groups = {table[pid][1] for pid in pids if pid in table} - {os.getpgid(0)}
    for group in groups:
        try:
            os.killpg(group, sig)
        except OSError:
            pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def stop_agent_processes() -> None:
    """Stop every process the agent left behind: SIGTERM, then SIGKILL after a grace period.

    Raises when any process is still alive, so the job fails before tests are restored.
    """
    me = os.getpid()
    grace = time.monotonic() + _REAP_GRACE_SECONDS
    deadline = time.monotonic() + _REAP_DEADLINE_SECONDS
    sig = signal.SIGTERM
    while True:
        _reap_zombies()
        left = _descendants(me)
        if not left:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("agent_processes_left")
        if sig == signal.SIGTERM and time.monotonic() >= grace:
            sig = signal.SIGKILL
        _signal_all(left, sig)
        time.sleep(0.05)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GradingViolation(RuntimeError):
    """The agent tampered with grading in a way that fails the job. `detail` is the case detail."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class StartupHookAdded(GradingViolation):
    """The agent added sitecustomize.py, usercustomize.py or a .pth file. It is deleted and the job fails."""


class ShadowModuleAdded(GradingViolation):
    """The agent added a top-level module or package named like stdlib or an installed package."""


def _remove_added_shadow_modules(work: Path, job: Path) -> list[str]:
    """Delete every NEW top-level entry of job that would shadow an import. Baseline names are kept."""
    removed: list[str] = []
    for path in sorted(job.iterdir()):
        name = path.name
        if os.path.lexists(work / name):
            continue
        probe = name if not path.is_dir() or path.is_symlink() else f"{name}/__init__.py"
        if grading_files.is_shadow_module(probe):
            _remove(path)
            removed.append(name)
    return removed


def _baseline_files(work: Path, editable: Collection[str] | None) -> dict[str, bytes]:
    """Every grading file of the job-creation workspace, by content. Caches are never baseline."""
    found: dict[str, bytes] = {}
    for name in sorted(grading_files.grading_paths(work, editable)):
        path = work / name
        if grading_files.is_cache_path(name) or path.is_symlink() or not path.is_file():
            continue
        found[name] = path.read_bytes()
    return found


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    for child in path.iterdir():
        _remove(child)
    path.rmdir()


def restore_grading_directory(work: Path, job: Path, editable: Collection[str] | None = None) -> None:
    """Make every grading file of the job directory match the baseline, file by file.

    Grading files are the denylist in aether_worker.grading_files (tests, oracle.py,
    conftest.py, pytest.ini, pyproject.toml, setup.cfg, tox.ini, startup hooks, caches).
    Grading files that existed at baseline are rewritten from it; grading files the agent
    added are deleted, and so is every symlink. Other source files keep the agent's edits
    and additions, except NEW top-level modules or packages that shadow stdlib, installed
    packages or verification._SHADOW_ROOTS names, which are deleted. The grading set is then
    verified against the baseline SHA-256 hashes. After the cleanup an added startup hook
    raises StartupHookAdded and an added shadow module raises ShadowModuleAdded.
    """
    baseline = _baseline_files(work, editable)
    expected = {name: _digest(data) for name, data in baseline.items()}
    hooks: list[str] = []
    shadows = _remove_added_shadow_modules(work, job)
    for path in sorted(job.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not os.path.lexists(path):
            continue
        name = path.relative_to(job).as_posix()
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            if grading_files.is_cache_path(name):
                _remove(path)
        elif grading_files.is_grading_path(name, editable) and name not in expected:
            if grading_files.is_startup_hook(name):
                hooks.append(name)
            _remove(path)
    for name, data in baseline.items():
        target = job / name
        if target.is_file() and not target.is_symlink() and _digest(target.read_bytes()) == expected[name]:
            continue
        if os.path.lexists(target):
            _remove(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _verify_grading_directory(job, expected, editable)
    if hooks:
        raise StartupHookAdded("grading_startup_hook")
    if shadows:
        raise ShadowModuleAdded("grading_shadow_module")


def _verify_grading_directory(job: Path, expected: dict[str, str], editable: Collection[str] | None) -> None:
    present = grading_files.grading_paths(job, editable)
    if any((job / name).is_symlink() for name in present):
        raise RuntimeError("grading_symlink")
    if present != set(expected):
        raise RuntimeError("grading_file_set_mismatch")
    for name in present:
        if _digest((job / name).read_bytes()) != expected[name]:
            raise RuntimeError("grading_hash_mismatch")


def main() -> None:
    started = time.monotonic()
    agent_report = _blank_report()
    try:
        become_subreaper()
        objective = job_objective()
        job = job_directory()
        copy_tree(WORK, job)
        before = _snapshot(job)
        agent_report = _run_agent(job, objective)
        stop_agent_processes()
        # Taken only once nothing the agent started can still write, and before grading files
        # are restored, so the published patch is the agent's source change that gets graded.
        # Grading files and caches are left out of it; tampering with them is caught separately.
        agent_report["patch"] = _patch(before, job)
        restore_grading_directory(WORK, job)
        workspace = LocalWorkspace(working_dir=str(job))
        result = workspace.execute_command(
            f"{sys.executable} -I {PROTOCOL}",
            cwd=str(job),
            timeout=40,
        )
        payload = _protocol_payload(result.stdout or "")
    except SystemExit:
        raise
    except ObjectiveMissing:
        emit(_failure("objective_missing"), 2)
    except GradingViolation as exc:
        # The offending file is already deleted. Exit 1 (a graded failure, not a runner crash) so the
        # worker records the job as FAILED and keeps the agent's patch for review.
        failure = _failure(exc.detail)
        failure.update(agent_report)
        emit(failure, 1)
    except Exception as exc:
        emit(_failure(type(exc).__name__), 2)
    payload.update(agent_report)
    payload["elapsed_seconds"] = round(time.monotonic() - started, 6)
    payload["network_errno"] = network_errno()
    payload["credential_present"] = credential_present()
    payload["executor"] = EXECUTOR
    failed = int(payload.get("failed") or 0)
    passed = int(payload.get("passed") or 0)
    code = 0 if failed == 0 and passed > 0 and result.exit_code == 0 and not result.timeout_occurred else 1
    if result.timeout_occurred:
        payload["errors"] = int(payload.get("errors") or 0) + 1
    emit(payload, code)


if __name__ == "__main__":
    main()
