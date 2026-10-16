"""The coding sandbox's OpenHands agent calls a scripted model and the ledger settles tokens."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.coding_jobs import CODING_RESERVE_MICRO_USD
from aether_worker.coding_sandbox import release_coding_container
from aether_worker.coding_workspace import unified_patch
from aether_worker.runner import docker_available
from aether_worker.service import route_pending
from tests.integration.test_console_records import _headers, _session

_PROVIDER_KEY = "gateway-provider-key"
_LOOPBACK = "loop-token-unit"
_SOURCE = "def value():\n    return 0\n"
WORKSPACE = {
    "kind": "aether.coding_workspace",
    "base_files": {"app.py": _SOURCE},
    "files": {"app.py": _SOURCE},
    "tests": {"test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n"},
}


def _require_docker() -> None:
    if not docker_available():
        pytest.fail("Docker is required for the OpenHands agent proof")


_ISOLATED_ENV = (
    "AETHER_GATEWAY_BASE",
    "AETHER_LOOPBACK_TOKEN",
    "AETHER_LIVE_MODEL_KEY",
    "AETHER_LIVE_MODEL_ALIAS",
    "AETHER_LIVE_MODEL_BASE",
    "AETHER_CODING_CALL_RESERVE_TOKENS",
    "AETHER_CODING_CALL_RESERVE_MICRO_USD",
)


class _FakeModel:
    def __init__(self, rounds: tuple[tuple[str, int, int], ...] | None = None) -> None:
        self.authorizations: list[str] = []
        self.models: list[str] = []
        self.tool_names: set[str] = set()
        self.usages: list[tuple[int, int]] = []
        self.problems: list[str] = []
        self._rounds = rounds or (("edit", 20, 11), ("finish", 20, 7))
        self._calls = 0
        self._lock = threading.Lock()

    def reply(self, raw: bytes, authorization: str) -> dict:
        with self._lock:
            self.authorizations.append(authorization)
            self._calls += 1
            call = self._calls
            loaded = json.loads(raw)
            model = loaded.get("model")
            if isinstance(model, str):
                self.models.append(model)
            for tool in loaded.get("tools") or []:
                function = tool.get("function") if isinstance(tool, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    self.tool_names.add(name)
            text_body = raw.decode("utf-8", "replace")
            if "aether_job_token" in loaded:
                self.problems.append("job token reached the model")
            if _PROVIDER_KEY in text_body:
                self.problems.append("provider key reached the model body")
        kind, prompt, completion = self._rounds[min(call, len(self._rounds)) - 1]
        target = _app_path(text_body)
        if kind == "edit" and not target.startswith("/"):
            self.problems.append("missing app.py path")
        if kind == "view":
            arguments = json.dumps({"command": "view", "path": target})
            name = "file_editor"
            call_id = "call_view"
        elif kind == "edit":
            arguments = json.dumps({"command": "str_replace", "path": target, "old_str": "return 0", "new_str": "return 42"})
            name = "file_editor"
            call_id = "call_edit"
        else:
            arguments = json.dumps({"message": "done"})
            name = "finish"
            call_id = "call_finish"
        with self._lock:
            self.usages.append((prompt, completion))
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
                        "tool_calls": [
                            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
        }


def _app_path(text: str) -> str:
    marker = "/app.py"
    start = text.rfind(marker)
    if start <= 0:
        return ""
    end = start + len(marker)
    begin = max(text.rfind(" ", 0, start), text.rfind('"', 0, start)) + 1
    return text[begin:end]


def _handler(model: _FakeModel) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            raw = self.rfile.read(length)
            payload = model.reply(raw, self.headers.get("authorization") or "")
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def _ready(proc: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + 20
    pending = ""
    errors = ""
    while time.monotonic() < deadline and "aether-role-ready port=" not in pending:
        streams = [item for item in (proc.stdout, proc.stderr) if item is not None]
        ready, _, _ = select.select(streams, [], [], 0.2)
        if proc.poll() is not None and not ready:
            break
        for stream in ready:
            text_chunk = os.read(stream.fileno(), 4096).decode("utf-8", "replace")
            if stream is proc.stdout:
                pending += text_chunk
            else:
                errors += text_chunk
    if "aether-role-ready port=" not in pending:
        raise AssertionError(f"gateway did not start: {pending} {errors}")
    line = next(item for item in pending.splitlines() if "port=" in item)
    port = line.rsplit("port=", 1)[1].strip()
    return f"http://127.0.0.1:{port}"


def test_openhands_agent_edits_through_the_gateway_and_settles_tokens(client, role_urls) -> None:
    _exercise(client, role_urls, _FakeModel(), "r8-agent")


def test_large_token_rounds_extend_the_reservation_and_settle_actual_usage(client, role_urls) -> None:
    rounds = (("view", 3000, 40), ("edit", 3100, 48), ("finish", 3200, 24))
    _exercise(client, role_urls, _FakeModel(rounds), "r8-large", minimum_requests=3)


def _exercise(client, role_urls, model: _FakeModel, prefix: str, *, minimum_requests: int = 2) -> None:
    _require_docker()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(model))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    saved = {name: os.environ.get(name) for name in _ISOLATED_ENV}
    gateway: subprocess.Popen[bytes] | None = None
    job_id = ""
    try:
        child_env = os.environ.copy()
        for name in _ISOLATED_ENV:
            child_env.pop(name, None)
            os.environ.pop(name, None)
        child_env["AETHER_LOOPBACK_TOKEN"] = _LOOPBACK
        child_env["AETHER_LIVE_MODEL_BASE"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
        gateway = subprocess.Popen(
            [sys.executable, "-m", "aether_gateway"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )
        assert gateway.stdin is not None
        gateway.stdin.write(
            (json.dumps({"provider_key": _PROVIDER_KEY, "worker_url": role_urls["aether_worker"]}) + "\n").encode()
        )
        gateway.stdin.close()
        os.environ["AETHER_GATEWAY_BASE"] = _ready(gateway)
        os.environ["AETHER_LOOPBACK_TOKEN"] = _LOOPBACK
        owner_id, project_id, csrf = _session(client, f"{prefix}-project")
        task = client.post(
            f"/v1/projects/{project_id}/tasks",
            headers=_headers(csrf, f"{prefix}-task"),
            json={"goal": "让 OpenHands 改到四十二"},
        )
        assert task.status_code == 202, task.text
        task_id = task.json()["resource_id"]
        uploaded = client.post(
            f"/v1/projects/{project_id}/artifacts",
            headers=_headers(csrf, f"{prefix}-file"),
            files={"file": ("workspace.json", json.dumps(WORKSPACE).encode(), "application/json")},
        )
        assert uploaded.status_code == 201, uploaded.text
        created = client.post(
            f"/v1/projects/{project_id}/coding-jobs",
            headers=_headers(csrf, f"{prefix}-job"),
            json={
                "task_id": task_id,
                "base_artifact_id": uploaded.json()["artifact_id"],
                "job_key": "agent",
                "objective": "把返回值改成 42",
            },
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["resource_id"]
        worker = Database(role_urls["aether_worker"])
        route_pending(
            Database(role_urls["aether_dispatcher"]),
            worker,
            Database(role_urls["aether_verifier"]),
            ArtifactStore(client.app.state.settings.artifact_root, worker),
        )
        _assert_settled(client, worker, owner_id, project_id, task_id, job_id, model, minimum_requests=minimum_requests)
    finally:
        if job_id:
            release_coding_container(job_id)
        if gateway is not None and gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gateway.kill()
        server.shutdown()
        server.server_close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _assert_settled(
    client,
    worker: Database,
    owner_id: str,
    project_id: str,
    task_id: str,
    job_id: str,
    model: _FakeModel,
    *,
    minimum_requests: int,
) -> None:
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT state, patch_artifact_id::text AS patch_id, result_artifact_id::text AS result_id, "
                "events_artifact_id::text AS events_id FROM coding_jobs "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": job_id},
        ).mappings().one()
        request = conn.execute(
            text(
                "SELECT usage_status, input_tokens, output_tokens, actual_micro_usd, reserved_micro_usd, usage_detail "
                "FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND role = 'coding'"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().one()
        account = conn.execute(
            text(
                "SELECT held_micro_usd, spent_micro_usd, tokens_used FROM budget_accounts "
                "WHERE project_id = :project_id AND task_id = :task_id"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().one()
    patch, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(row["patch_id"]))
    result_bytes, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(row["result_id"]))
    events_bytes, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(row["events_id"]))
    result = json.loads(result_bytes)
    events = json.loads(events_bytes)
    detail = result_bytes.decode("utf-8", "replace") + events_bytes.decode("utf-8", "replace") + json.dumps(model.problems)
    assert row["state"] == "FINISHED", detail
    assert model.problems == []
    candidate = unified_patch(WORKSPACE["base_files"], WORKSPACE["files"])
    assert "return 42" not in candidate
    assert b"return 42" in patch
    assert result["edit_calls"] >= 1
    assert result["model_requests"] >= minimum_requests
    assert result["agent_completed"] is True
    assert result["executor"] == "openhands.sdk.Agent"
    incoming = sum(item[0] for item in model.usages)
    outgoing = sum(item[1] for item in model.usages)
    assert result["input_tokens"] == incoming
    assert result["output_tokens"] == outgoing
    assert events["network"] == "none"
    assert events["read_only"] is True
    assert events["credential_present"] is False
    assert events["network_errno"] not in {0, None}
    assert events["edit_calls"] >= 1
    assert model.authorizations
    assert set(model.authorizations) == {f"Bearer {_PROVIDER_KEY}"}
    assert set(model.models) == {"coder"}
    assert "file_editor" in model.tool_names
    assert "terminal" in model.tool_names
    visible = patch + result_bytes + events_bytes
    assert _PROVIDER_KEY.encode() not in visible
    detail = request["usage_detail"]
    assert request["usage_status"] == "known"
    assert int(request["reserved_micro_usd"]) >= CODING_RESERVE_MICRO_USD
    assert int(request["reserved_micro_usd"]) >= incoming + outgoing
    assert int(request["input_tokens"]) == incoming
    assert int(request["output_tokens"]) == outgoing
    assert int(request["actual_micro_usd"]) == incoming + outgoing
    assert detail["book_result"] == "settled"
    assert int(detail.get("reservation_extensions") or 0) >= 1
    assert int(account["held_micro_usd"]) == 0
    assert int(account["spent_micro_usd"]) == incoming + outgoing
    assert int(account["tokens_used"]) == incoming + outgoing
