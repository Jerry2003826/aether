"""Sandbox entry. OpenHands Agent edits the workspace, then LocalWorkspace runs the protocol."""

from __future__ import annotations

import difflib
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import openhands.tools.file_editor as file_editor_tool
import openhands.tools.terminal as terminal_tool
from openhands.sdk import Agent, Conversation, ConversationExecutionStatus, LLM, Tool
from openhands.sdk.event import ActionEvent
from openhands.sdk.workspace.local import LocalWorkspace

WORK = Path(os.environ.get("AETHER_SANDBOX_WORK", "/work"))
PROTOCOL = Path(os.environ.get("AETHER_SANDBOX_PROTOCOL", "/opt/aether/coding_protocol.py"))
EXECUTOR = "openhands.sdk.Agent"
_STATE_DIR = ".aether-state"
_SKIP = {".git", "__pycache__", _STATE_DIR}


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
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                return

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
    after = _snapshot(root)
    chunks: list[str] = []
    for name in sorted(set(before) | set(after)):
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
    return {"input_tokens": 0, "output_tokens": 0, "edit_calls": 0, "model_requests": 0, "patch": ""}


def _collect(agent: Agent, conversation: Conversation, before: dict[str, str], job: Path) -> dict[str, object]:
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
        "patch": _patch(before, job),
    }


def _run_agent(job: Path, before: dict[str, str]) -> dict[str, object]:
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
            system_prompt="Edit the requested file with file_editor, then call finish. Do not use any other network.",
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
        target = job / "app.py"
        conversation.send_message(f"Edit {target} with file_editor str_replace so it returns 42, then call finish.")
        conversation.run()
        if conversation.state.execution_status == ConversationExecutionStatus.FINISHED:
            report = _collect(agent, conversation, before, job)
    except Exception as exc:
        sys.stderr.write(f"agent_failed {type(exc).__name__}\n")
        return report
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    return report


def main() -> None:
    started = time.monotonic()
    try:
        job = job_directory()
        copy_tree(WORK, job)
        before = _snapshot(job)
        agent_report = _run_agent(job, before)
        workspace = LocalWorkspace(working_dir=str(job))
        result = workspace.execute_command(
            f"{sys.executable} -I {PROTOCOL}",
            cwd=str(job),
            timeout=40,
        )
        payload = _protocol_payload(result.stdout or "")
    except SystemExit:
        raise
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
