"""Sandbox entry. OpenHands Agent edits the workspace, then LocalWorkspace runs the protocol."""

from __future__ import annotations

import ctypes
import difflib
import hashlib
import json
import os
import secrets
import shlex
import shutil
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
# The sandbox's shared memory (the launcher sets /dev/shm). Required: the entry empties it after the
# agent stopped, so it never guesses one (and never empties a directory nobody named).
_SHM_ENV = "AETHER_SANDBOX_SHM"
# Seconds past the protocol's own budget before the entry kills it (the protocol stops 3 s before it).
_PROTOCOL_KILL_MARGIN_SECONDS = 5
EXECUTOR = "openhands.sdk.Agent"
_STATE_DIR = ".aether-state"
_OBJECTIVE = "AETHER_OBJECTIVE"
_SKIP = {".git", "__pycache__", _STATE_DIR}
# Same value as coding_sandbox.MAX_PATCH_BYTES: the RESULT line must fit the worker's stdout window.
MAX_PATCH_BYTES = 2_097_152
# The worker keeps only the last output_bounds.RESULT_CAPTURE_LIMIT (4 * MAX_PATCH_BYTES) bytes of stdout.
# JSON escaping can grow a patch up to sixfold (a control character becomes \u00XX), so the serialized
# line is capped too, with room for the newline and any trailing output.
MAX_RESULT_LINE_BYTES = 4 * MAX_PATCH_BYTES - 65536


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


# The agent phase's model channel. coding_model_proxy.REVOKE_PATH is the host side of this literal.
_MODEL_ENV = ("AETHER_MODEL_SOCKET", "AETHER_JOB_TOKEN", "AETHER_MODEL_ALIAS")
_REVOKE_PATH = "/aether/revoke"
_REVOKE_TIMEOUT_SECONDS = 5.0


class ModelChannelOpen(RuntimeError):
    """The model channel could not be confirmed closed, so no candidate code may run for grading.

    Not a grading violation: nothing shows the agent tampered (it cannot remove or keep the socket;
    the host owns it). It is our side failing to close the channel, so the job fails with its own
    code `model_channel_open` and grading is skipped, never run with the channel still open.
    """


def _revoke_status(socket_path: str, token: str, timeout: float) -> int | None:
    """HTTP status of the revoke request, or None if it could not be sent or got no status line."""
    request = (
        f"POST {_REVOKE_PATH} HTTP/1.1\r\nhost: aether-proxy\r\nauthorization: Bearer {token}\r\n"
        "content-length: 0\r\nconnection: close\r\n\r\n"
    ).encode("ascii")
    chunks: list[bytes] = []
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(timeout)
            channel.connect(socket_path)
            channel.sendall(request)
            while block := channel.recv(4096):
                chunks.append(block)
    except OSError:
        return None
    parts = b"".join(chunks).split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        return None
    return int(parts[1])


def close_model_channel() -> None:
    """Take the model channel away before any candidate code runs for grading; fail closed.

    The agent phase knew the job token and socket path and may have written them into the
    workspace, so dropping them from this process's environment is not enough: the host proxy
    must revoke the job. It is asked (with the token) to do so; it refuses later model calls and
    unlinks the socket, which this unprivileged process could not remove itself. Only an HTTP 200
    followed by the socket file disappearing within the timeout counts as closed; anything else
    raises ModelChannelOpen and grading does not run. With no channel configured there is nothing
    to close.
    """
    socket_path = os.environ.get("AETHER_MODEL_SOCKET", "")
    token = os.environ.get("AETHER_JOB_TOKEN", "")
    for name in _MODEL_ENV:
        os.environ.pop(name, None)
    if not socket_path and not token:
        return
    deadline = time.monotonic() + _REVOKE_TIMEOUT_SECONDS
    status = _revoke_status(socket_path, token, _REVOKE_TIMEOUT_SECONDS) if socket_path and token else None
    while status == 200 and os.path.lexists(socket_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    if status != 200 or os.path.lexists(socket_path):
        sys.stderr.write(f"model_channel_open status={status}\n")
        raise ModelChannelOpen("model_channel_open")


def credential_present() -> bool:
    markers = ("LIVE_MODEL", "ADMIN_DATABASE", "PASSWORD", "SECRET")
    return any(any(part in name for part in markers) for name in os.environ)


def emit(body: dict[str, object], code: int) -> None:
    line = "RESULT " + json.dumps(body, separators=(",", ":"))
    patch = body.get("patch")
    if isinstance(patch, str) and patch and (len(patch.encode("utf-8")) > MAX_PATCH_BYTES or len(line) > MAX_RESULT_LINE_BYTES):
        # Too large raw, or escaped past the worker's window (a cut line would lose the RESULT prefix
        # and read as sandbox_failed). The patch is dropped; result_line_bytes is the size of the line
        # it would have needed (ASCII: json.dumps escapes everything else), for the job log.
        body["patch"] = ""
        body["patch_omitted"] = "too_large"
        body["result_line_bytes"] = len(line)
        line = "RESULT " + json.dumps(body, separators=(",", ":"))
    sys.stdout.write(line + "\n")
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


class GradingTimeout(RuntimeError):
    """The protocol produced no result line: it hung past its limit, was killed or crashed.

    A graded failure (grading_timeout), not tampering: nothing was forged.
    """

    detail = "grading_timeout"


def _protocol_payload(stdout: str, nonce: str) -> dict[str, object]:
    """The protocol's result: the one stdout line signed with this run's nonce.

    Candidate code cannot learn the nonce (the protocol reads and deletes its file before any candidate
    code runs), so a forged (unsigned) or duplicated result fails the job as grading_unverified. No result
    line at all is grading_timeout.
    """
    signed: list[dict[str, object]] = []
    unsigned = 0
    for line in stdout.splitlines():
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("nonce") == nonce:
            signed.append(body)
        elif isinstance(body, dict) and "cases" in body:
            unsigned += 1
    if not signed and not unsigned:
        raise GradingTimeout()
    if len(signed) != 1:
        raise GradingViolation("grading_unverified")
    body = signed[0]
    body.pop("nonce", None)
    return body


_PR_SET_DUMPABLE = 4


def not_dumpable() -> None:
    """Keep same-uid processes started during grading out of /proc/<entry>/{fd,mem,environ} (best effort)."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _nonce_file(nonce: str) -> Path:
    """A private file holding the nonce; the protocol deletes it before it runs any candidate code."""
    folder = Path(tempfile.mkdtemp(prefix="aether-nonce-"))
    path = folder / "nonce"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(nonce)
    return path


class SharedMemoryUnavailable(RuntimeError):
    """The shared memory is not named, or could not be emptied: nothing is graded."""


def shared_memory_root() -> Path:
    """The sandbox's shared-memory directory, as the launcher named it (AETHER_SANDBOX_SHM)."""
    value = os.environ.get(_SHM_ENV, "")
    path = Path(value)
    if not value or not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise SharedMemoryUnavailable("shared_memory_unconfigured")
    return path


def _purge(path: Path) -> None:
    """Delete path and everything under it, whatever modes the agent set; links are removed, never followed."""
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    os.chmod(path, 0o700)
    for child in path.iterdir():
        _purge(child)
    path.rmdir()


def clear_shared_memory(root: Path) -> None:
    """Empty the shared memory the agent and the candidate share, once no agent process is left.

    The agent may have left anything there (a copy of the tests or the oracle); the candidate uses the
    same directory for its semaphores and shared memory, so it starts from an empty one. Anything that
    cannot be removed fails the job before any candidate code runs.
    """
    for child in list(root.iterdir()):
        try:
            _purge(child)
        except OSError:
            pass
    if any(root.iterdir()):
        raise SharedMemoryUnavailable("shared_memory_not_cleared")


def job_directory() -> Path:
    """Use the caller path, or a private directory from tempfile. A fixed public path is not created."""
    configured = os.environ.get("AETHER_SANDBOX_JOB")
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="aether-job-"))


def _snapshot(root: Path) -> dict[str, str]:
    """Text of every regular file. Present-but-empty is "", absent is a missing key."""
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
    # Marked root helpers are judged from the read-only baseline (WORK), never from the agent's copy.
    marked = grading_files.marked_grading_files(WORK)
    for name in sorted(set(before) | set(after)):
        if grading_files.is_grading_path(name, marked=marked) or grading_files.is_cache_path(name):
            continue
        old = before.get(name)
        new = after.get(name)
        if old == new:
            continue
        # Existence alone picks the headers, as in diff -N / git: a created file comes from /dev/null and
        # a deleted one goes to it, so applying the patch removes it (not leaves an empty file); a file
        # present on both sides (emptied included) is a modification of a/ into b/.
        source = f"a/{name}" if old is not None else "/dev/null"
        target = f"b/{name}" if new is not None else "/dev/null"
        if not old and not new:
            # An empty file created or deleted: no lines to diff, but it is a change.
            chunks.append(f"--- {source}\n")
            chunks.append(f"+++ {target}\n")
            continue
        chunks.extend(difflib.unified_diff(_lines(old or ""), _lines(new or ""), fromfile=source, tofile=target))
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
    """The agent added, or changed, a top-level module or package named like stdlib or an installed package."""


def _tree_digests(path: Path) -> dict[str, str]:
    """Relative path -> digest of every file under a top-level entry (caches left out); a symlink counts as changed."""
    if path.is_symlink():
        return {"": "symlink"}
    if not path.is_dir():
        return {"": _digest(path.read_bytes())}
    found: dict[str, str] = {}
    for item in path.rglob("*"):
        relative = item.relative_to(path).as_posix()
        if grading_files.is_cache_path(relative):
            continue
        if item.is_symlink():
            found[relative] = "symlink"
        elif item.is_file():
            found[relative] = _digest(item.read_bytes())
    return found


def _restore_entry(work: Path, job: Path) -> None:
    if os.path.lexists(job):
        _remove(job)
    if work.is_dir():
        shutil.copytree(work, job, symlinks=True)
    else:
        shutil.copy2(work, job)


def _remove_added_shadow_modules(work: Path, job: Path) -> list[str]:
    """Delete every NEW top-level entry of job that would shadow an import, and restore every BASELINE one
    that the agent changed. Both are reported; an unchanged baseline module with such a name is kept (the
    protocol never serves it under that name, see coding_protocol._module_map)."""
    flagged: list[str] = []
    for path in sorted(job.iterdir()):
        name = path.name
        probe = name if not path.is_dir() or path.is_symlink() else f"{name}/__init__.py"
        if not grading_files.is_shadow_module(probe) and not grading_files.is_shadow_module(f"{name}/__init__.py"):
            continue
        baseline = work / name
        if not os.path.lexists(baseline):
            _remove(path)
            flagged.append(name)
        elif _tree_digests(path) != _tree_digests(baseline):
            _restore_entry(baseline, path)
            flagged.append(name)
    return flagged


def _baseline_files(work: Path, editable: Collection[str] | None, marked: frozenset[str]) -> dict[str, bytes]:
    """Every grading file of the job-creation workspace, by content. Caches are never baseline."""
    found: dict[str, bytes] = {}
    for name in sorted(grading_files.grading_paths(work, editable, marked)):
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
    conftest.py, pytest.ini, pyproject.toml, setup.cfg, tox.ini, startup hooks, caches), plus
    the root helpers the baseline marks with GRADING_MARKER (see marked_grading_files).
    Grading files that existed at baseline are rewritten from it; grading files the agent
    added are deleted, and so is every symlink. Other source files keep the agent's edits
    and additions, except NEW top-level modules or packages that shadow stdlib, installed
    packages or verification._SHADOW_ROOTS names, which are deleted. The grading set is then
    verified against the baseline SHA-256 hashes. After the cleanup an added startup hook
    raises StartupHookAdded and an added shadow module raises ShadowModuleAdded.
    """
    # Root helpers marked in the baseline (`# aether: grading` on line one, or line two after a shebang
    # or coding line) are grading files; the job copy's marker does not count, so removing the marker
    # changes nothing and adding one gains nothing.
    marked = grading_files.marked_grading_files(work)
    baseline = _baseline_files(work, editable, marked)
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
        elif grading_files.is_grading_path(name, editable, marked) and name not in expected:
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
    _verify_grading_directory(job, expected, editable, marked)
    if hooks:
        raise StartupHookAdded("grading_startup_hook")
    if shadows:
        raise ShadowModuleAdded("grading_shadow_module")


def _verify_grading_directory(
    job: Path, expected: dict[str, str], editable: Collection[str] | None, marked: frozenset[str] = frozenset()
) -> None:
    present = grading_files.grading_paths(job, editable, marked)
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
        shared_memory = shared_memory_root()
        job = job_directory()
        copy_tree(WORK, job)
        before = _snapshot(job)
        agent_report = _run_agent(job, objective)
        stop_agent_processes()
        # No agent process is left to write it again: from here the candidate's shared memory is empty.
        clear_shared_memory(shared_memory)
        # Taken only once nothing the agent started can still write, and before grading files
        # are restored, so the published patch is the agent's source change that gets graded.
        # Grading files and caches are left out of it; tampering with them is caught separately.
        agent_report["patch"] = _patch(before, job)
        # Before any candidate code runs: no grading with the model channel still open.
        close_model_channel()
        restore_grading_directory(WORK, job)
        not_dumpable()
        nonce = secrets.token_hex(16)
        secret = _nonce_file(nonce)
        # The limit grows with the suite (one fresh process per test), under a hard cap; the protocol
        # gets the same budget and stops starting tests just before it, so it still reports. Counted
        # in the read-only baseline WORK, never in the agent's copy, so the agent cannot inflate it.
        budget = grading_files.protocol_timeout_seconds(grading_files.grading_steps(WORK))
        try:
            workspace = LocalWorkspace(working_dir=str(job))
            result = workspace.execute_command(
                f"AETHER_PROTOCOL_BUDGET_SECONDS={budget} AETHER_PROTOCOL_BASELINE_DIR={shlex.quote(str(WORK))} AETHER_PROTOCOL_SHM_DIR={shlex.quote(str(shared_memory))} AETHER_PROTOCOL_NONCE_FILE={shlex.quote(str(secret))} {sys.executable} -I {PROTOCOL}",
                cwd=str(job),
                timeout=budget + _PROTOCOL_KILL_MARGIN_SECONDS,
            )
        finally:
            secret.unlink(missing_ok=True)
            secret.parent.rmdir()
            # Nothing the protocol started may outlive it, on the timeout path too.
            stop_agent_processes()
        payload = _protocol_payload(result.stdout or "", nonce)
    except SystemExit:
        raise
    except ObjectiveMissing:
        emit(_failure("objective_missing"), 2)
    except SharedMemoryUnavailable as exc:
        emit(_failure(str(exc)), 2)
    except ModelChannelOpen as exc:
        # Exit 1 like a graded failure, so the worker records FAILED/model_channel_open with the patch.
        failure = _failure(str(exc))
        failure.update(agent_report)
        emit(failure, 1)
    except GradingTimeout as exc:
        # Exit 1 like a failed test: a graded failure that is not tampering, with its own detail.
        failure = _failure(exc.detail)
        failure.update(agent_report)
        emit(failure, 1)
    except GradingViolation as exc:
        # The offending file is already deleted. Exit 1 (a graded failure, not a runner crash) so the
        # worker records the job as FAILED and keeps the agent's patch for review.
        failure = _failure(exc.detail)
        failure.update(agent_report)
        emit(failure, 1)
    except Exception as exc:
        # Only the type, never the message: Docker's own json log keeps container output until the
        # container is removed, and an unconfirmed stop, a worker crash or a failed rm -f can leave
        # it; no redaction reaches that copy (see coding_sandbox._log_tails for the stored tails).
        sys.stderr.write(f"entry_failed {type(exc).__name__}\n")
        sys.stderr.flush()
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
