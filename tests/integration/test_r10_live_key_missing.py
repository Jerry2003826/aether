"""A research task with no provider key is refused unsent, holds no reservation, and resumes once fixed.

Codex (role_launch.py:100): a gateway running without AETHER_LIVE_MODEL_KEY used to admit research;
the provider's 401 was booked as a sent call with unknown usage and the task could not be resumed.
Here a real gateway process is started without a key (what the launcher did before the fix, and what a
manual start can still do), against a local upstream that answers 401 without the right key, like a
real provider.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.goal_effect import WorkerEndpoints, materialize_goal_effect
from aether_worker.outbox import enqueue_task
from sqlalchemy import text

from tests.integration.goal_approval import record_owner_approval
from tests.integration.test_artifacts import _project
from tests.integration.test_live_research import FACT, GOAL
from tests.integration.test_openhands_agent_gateway import _ready

_KEY = "local-upstream-key-r10"
_LOOPBACK = "loopback-r10-live-key"


class _Upstream(BaseHTTPRequestHandler):
    seen: ClassVar[list[str | None]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or "0")
        self.rfile.read(length)
        authorization = self.headers.get("authorization")
        _Upstream.seen.append(authorization)
        if authorization != f"Bearer {_KEY}":
            status, payload = 401, {"error": {"message": "invalid api key", "type": "invalid_request_error"}}
        else:
            status, payload = 200, {
                "id": "chatcmpl-r10",
                "object": "chat.completion",
                "model": "fake-upstream-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"资料记载：{FACT}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        return


def _gateway(role_urls, base: str, key: str) -> tuple[subprocess.Popen[bytes], str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AETHER_")}
    env.update({"AETHER_LOOPBACK_TOKEN": _LOOPBACK, "AETHER_LIVE_MODEL_BASE": base, "AETHER_PROCESS_ROLE": "gateway"})
    proc = subprocess.Popen(
        [sys.executable, "-m", "aether_gateway"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    assert proc.stdin is not None
    proc.stdin.write((json.dumps({"provider_key": key, "worker_url": role_urls["aether_worker"]}) + "\n").encode())
    proc.stdin.close()
    return proc, _ready(proc)


def _endpoints(role_urls, root: str, base: str, gateway_base: str) -> WorkerEndpoints:
    # The worker itself never holds the key; it reaches the model only through the gateway.
    return WorkerEndpoints(
        worker_url=role_urls["aether_worker"],
        verifier_url=role_urls["aether_verifier"],
        dispatcher_url=role_urls["aether_dispatcher"],
        artifact_root=root,
        temporal_address="127.0.0.1:9",
        task_queue="r10-live-key",
        research_provider="live",
        live_model_base=base,
        live_model_alias="research-alias",
        live_model_key="",
        live_timeout_seconds=5,
        gateway_base=gateway_base,
        loopback_token=_LOOPBACK,
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_missing_key_is_refused_unsent_uncharged_and_resumable(client, role_urls) -> None:
    owner, project, headers = _project(client)
    worker = Database(role_urls["aether_worker"])
    root = client.app.state.settings.artifact_root
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    keyless, keyless_base = _gateway(role_urls, base, "")
    keyed: subprocess.Popen[bytes] | None = None
    try:
        store = ArtifactStore(root, worker)
        visible = store.put(owner_id=owner, project_id=project, data=f"{FACT}\n".encode(), media_type="text/plain")
        created = enqueue_task(
            worker, owner_id=owner, project_id=project, goal=GOAL, command_key=str(uuid.uuid4()),
            allowed_source_ids=[str(visible["artifact_id"])],
        )
        record_owner_approval(worker, owner_id=owner, project_id=project, task_id=created["task_id"], workflow_id=created["workflow_id"])
        payload = {"project_id": project, "task_id": created["task_id"], "command_id": created["command_key"]}

        result = materialize_goal_effect(_endpoints(role_urls, root, base, keyless_base), payload)

        # Explicit, key-missing, and nothing reached the provider.
        assert result.get("stop_reason") == "live_provider_unconfigured", result
        assert _Upstream.seen == []
        with worker.transaction(owner_id=owner, project_id=project) as conn:
            task = conn.execute(text("SELECT status, resume_state FROM tasks WHERE id = :id"), {"id": created["task_id"]}).mappings().one()
            review = conn.execute(
                text("SELECT payload FROM task_events WHERE task_id = :id AND event_type = 'task.needs_review' ORDER BY sequence DESC LIMIT 1"),
                {"id": created["task_id"]},
            ).scalar_one()
            requests = conn.execute(
                text("SELECT usage_status, actual_micro_usd FROM model_requests WHERE task_id = :id"), {"id": created["task_id"]}
            ).mappings().all()
            account = conn.execute(
                text("SELECT spent_micro_usd, held_micro_usd, tokens_used FROM budget_accounts WHERE task_id = :id"),
                {"id": created["task_id"]},
            ).mappings().first()
        review = review if isinstance(review, dict) else json.loads(review)
        assert task["status"] == "NEEDS_REVIEW", task
        assert str(task["resume_state"] or "").startswith("review:"), task
        assert review["missing"] == ["AETHER_LIVE_MODEL_KEY"], review
        assert _KEY not in json.dumps(review)
        # Not counted as sent, nothing charged: any reservation was released before dispatch.
        assert all(row["usage_status"] == "cancelled_before_dispatch" for row in requests), requests
        assert all(row["actual_micro_usd"] is None for row in requests), requests
        if account is not None:
            assert (account["spent_micro_usd"], account["held_micro_usd"], account["tokens_used"]) == (0, 0, 0), account

        # The key is configured: a gateway with it. The owner then approves the review through the
        # API, exactly as the console does, and the worker runs the resolve_review command the API
        # queued (what the workflow delivers): back to the stage it stopped in, then the research.
        _stop(keyless)
        keyed, keyed_base = _gateway(role_urls, base, _KEY)
        listed = client.get(f"/v1/projects/{project}/approvals")
        assert listed.status_code == 200, listed.text
        pending = [item for item in listed.json()["items"] if item["task_id"] == created["task_id"]]
        assert len(pending) == 1, pending
        approval = pending[0]
        assert approval["normalized_payload"]["action"] == "resolve_review", approval
        assert approval["normalized_payload"]["code"] == "live_provider_unconfigured", approval
        decided = client.post(
            f"/v1/projects/{project}/approvals/{approval['id']}/decision",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4()), "If-Match": str(approval["version"])},
            json={"decision": "approve", "expected_intent_hash": approval["intent_digest"]},
        )
        assert decided.status_code == 202, decided.text
        resolve_key = f"resolve-{approval['id']}"
        with worker.transaction(owner_id=owner, project_id=project) as conn:
            queued = conn.execute(
                text("SELECT kind FROM command_outbox WHERE project_id = :project AND task_id = :id AND command_key = :key"),
                {"project": project, "id": created["task_id"], "key": resolve_key},
            ).scalar_one()
        assert queued == "resolve_review"
        resolve = {**payload, "command_id": resolve_key}
        resumed = materialize_goal_effect(_endpoints(role_urls, root, base, keyed_base), resolve)
        assert resumed.get("status") == "SUCCEEDED", resumed
        assert _Upstream.seen == [f"Bearer {_KEY}"]
    finally:
        for proc in (keyless, keyed):
            if proc is not None and proc.poll() is None:
                _stop(proc)
        server.shutdown()
