"""Live research path against a local OpenAI-compatible server. No provider key is used."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker.goal_effect import WorkerEndpoints, materialize_goal_effect
from aether_worker.outbox import enqueue_task
from tests.integration.test_artifacts import _project

FACT = "事实：水在标准大气压下于一百摄氏度沸腾"
SECRET = "秘密句子不应进入模型上下文"
GOAL = "请根据已上传资料回答沸点"
KEY = "local-upstream-token"


class Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []
    mode = "ok"
    body_text = ""
    delay = 0.0
    model_name = "fake-upstream-model"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length)
        Upstream.seen.append({"path": self.path, "authorization": self.headers.get("authorization"), "body": json.loads(raw.decode())})
        if Upstream.delay:
            time.sleep(Upstream.delay)
        if Upstream.mode == "error":
            payload = b'{"error":{"message":"upstream"}}'
            status = 500
        else:
            payload = json.dumps(
                {
                    "id": "chatcmpl-local",
                    "object": "chat.completion",
                    "model": Upstream.model_name,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": Upstream.body_text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
            ).encode()
            status = 200
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def _server(*, mode: str = "ok", body: str | None = None, delay: float = 0.0):
    Upstream.seen = []
    Upstream.mode = mode
    Upstream.body_text = f"资料记载：{FACT}" if body is None else body
    Upstream.delay = delay
    Upstream.model_name = "fake-upstream-model"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _endpoints(role_urls, root, base: str, *, key: str = KEY, timeout: float = 5) -> WorkerEndpoints:
    return WorkerEndpoints(
        worker_url=role_urls["aether_worker"],
        verifier_url=role_urls["aether_verifier"],
        dispatcher_url=role_urls["aether_dispatcher"],
        artifact_root=root,
        temporal_address="127.0.0.1:9",
        task_queue="live-research-direct",
        research_provider="live",
        live_model_base=base,
        live_model_alias="research-alias",
        live_model_key=key,
        live_timeout_seconds=timeout,
    )


def _run(client, role_urls, *, budget=None, allowed=None, key: str = KEY, timeout: float = 5, backdate: bool = False, mode: str = "ok", body: str | None = None, delay: float = 0.0):
    owner, project, _headers = _project(client)
    worker = Database(role_urls["aether_worker"])
    root = client.app.state.settings.artifact_root
    server = _server(mode=mode, body=body, delay=delay)
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        store = ArtifactStore(root, worker)
        visible = store.put(owner_id=owner, project_id=project, data=f"{FACT}\n".encode(), media_type="text/plain")
        store.put(owner_id=owner, project_id=project, data=f"{SECRET}\n".encode(), media_type="text/plain")
        created = enqueue_task(
            worker,
            owner_id=owner,
            project_id=project,
            goal=GOAL,
            command_key=str(uuid.uuid4()),
            budget=budget,
            allowed_source_ids=allowed if allowed is not None else [str(visible["artifact_id"])],
        )
        if backdate:
            with worker.transaction(owner_id=owner, project_id=project) as conn:
                conn.execute(
                    text("UPDATE tasks SET created_at = :created WHERE project_id = :project AND id = :id"),
                    {"created": datetime.now(UTC) - timedelta(hours=2), "project": project, "id": created["task_id"]},
                )
        result = materialize_goal_effect(
            _endpoints(role_urls, root, base, key=key, timeout=timeout),
            {"project_id": project, "task_id": created["task_id"], "command_id": created["command_key"]},
        )
        with worker.transaction(owner_id=owner, project_id=project) as conn:
            status = conn.execute(text("SELECT status, resume_state FROM tasks WHERE id = :id"), {"id": created["task_id"]}).mappings().one()
            events = conn.execute(
                text("SELECT event_type, payload FROM task_events WHERE project_id = :project AND task_id = :id ORDER BY sequence"),
                {"project": project, "id": created["task_id"]},
            ).mappings().all()
        return result, dict(status), [dict(row) for row in events], str(visible["artifact_id"])
    finally:
        server.shutdown()


def test_live_answer_quotes_the_source_and_records_the_upstream_model(client, role_urls) -> None:
    result, status, events, _artifact = _run(client, role_urls)
    assert result["status"] == "SUCCEEDED"
    assert status["status"] == "SUCCEEDED"
    call = next(row for row in events if row["event_type"] == "research.call")
    payload = call["payload"] if isinstance(call["payload"], dict) else json.loads(call["payload"])
    assert payload["model_mode"] == "live"
    assert payload["offline"] is False
    assert payload["requested_alias"] == "research-alias"
    assert payload["upstream_model"] == "fake-upstream-model"
    assert payload["input_tokens"] == 11
    assert payload["output_tokens"] == 7
    assert payload["cost_status"] == "unknown"
    assert payload["cost_micro_usd"] is None
    assert KEY not in json.dumps(payload)
    assert Upstream.seen[0]["path"].endswith("/chat/completions")
    assert Upstream.seen[0]["authorization"] == f"Bearer {KEY}"
    assert Upstream.seen[0]["body"]["model"] == "research-alias"


def test_wrong_live_answer_is_not_succeeded(client, role_urls) -> None:
    result, status, _events, _artifact = _run(client, role_urls, body="这是一段没有引用资料的回答。")
    assert result["status"] != "SUCCEEDED"
    assert status["status"] != "SUCCEEDED"


def test_budget_stops_before_the_upstream_call(client, role_urls) -> None:
    result, status, events, _artifact = _run(
        client,
        role_urls,
        budget={"max_cost_micro_usd": 2_000_000, "max_tokens": 1, "max_model_requests": 4, "active_seconds": 1800, "verification_reserve_fraction": 0},
    )
    assert result["stop_reason"] == "budget_exhausted"
    assert status["status"] == "FAILED"
    assert status["resume_state"] == "budget_exhausted"
    assert any(row["event_type"] == "task.budget_exhausted" for row in events)
    assert Upstream.seen == []


def test_cost_and_call_and_wall_budgets_do_not_call_upstream(client, api_db, role_urls) -> None:
    owner, project, _headers = _project(client)
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    visible = store.put(owner_id=owner, project_id=project, data=f"{FACT}\n".encode(), media_type="text/plain")
    server = _server()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        cost_task = enqueue_task(
            worker,
            owner_id=owner,
            project_id=project,
            goal=GOAL,
            command_key=str(uuid.uuid4()),
            budget={"max_cost_micro_usd": 0, "max_tokens": 1000, "max_model_requests": 4, "active_seconds": 1800, "verification_reserve_fraction": 0},
            allowed_source_ids=[str(visible["artifact_id"])],
        )
        cost = materialize_goal_effect(
            _endpoints(role_urls, store.root, base),
            {"project_id": project, "task_id": cost_task["task_id"], "command_id": cost_task["command_key"]},
        )
        assert cost["stop_reason"] == "budget_exhausted"
        call_task = enqueue_task(
            worker,
            owner_id=owner,
            project_id=project,
            goal=GOAL,
            command_key=str(uuid.uuid4()),
            budget={"max_cost_micro_usd": 2_000_000, "max_tokens": 1000, "max_model_requests": 1, "active_seconds": 1800, "verification_reserve_fraction": 0},
            allowed_source_ids=[str(visible["artifact_id"])],
        )
        BudgetLedger(worker).open_account(
            owner_id=owner,
            project_id=project,
            task_id=call_task["task_id"],
            limit_micro_usd=2_000_000,
            token_limit=1000,
            max_requests=1,
            verification_reserve_fraction=0,
            active_seconds=1800,
        )
        with worker.transaction(owner_id=owner, project_id=project) as conn:
            conn.execute(text("UPDATE budget_accounts SET requests_admitted = 1 WHERE task_id = :id"), {"id": call_task["task_id"]})
        calls = materialize_goal_effect(
            _endpoints(role_urls, store.root, base),
            {"project_id": project, "task_id": call_task["task_id"], "command_id": call_task["command_key"]},
        )
        assert calls["stop_reason"] == "budget_exhausted"
        wall_task = enqueue_task(
            worker,
            owner_id=owner,
            project_id=project,
            goal=GOAL,
            command_key=str(uuid.uuid4()),
            budget={"max_cost_micro_usd": 2_000_000, "max_tokens": 1000, "max_model_requests": 4, "active_seconds": 1, "verification_reserve_fraction": 0},
            allowed_source_ids=[str(visible["artifact_id"])],
        )
        with worker.transaction(owner_id=owner, project_id=project) as conn:
            conn.execute(
                text("UPDATE tasks SET created_at = :created WHERE id = :id"),
                {"created": datetime.now(UTC) - timedelta(seconds=5), "id": wall_task["task_id"]},
            )
        wall = materialize_goal_effect(
            _endpoints(role_urls, store.root, base),
            {"project_id": project, "task_id": wall_task["task_id"], "command_id": wall_task["command_key"]},
        )
        assert wall["stop_reason"] == "budget_exhausted"
        assert Upstream.seen == []
    finally:
        server.shutdown()


def test_allowed_sources_hide_other_documents(client, role_urls) -> None:
    result, _status, _events, _artifact = _run(client, role_urls)
    assert result["status"] == "SUCCEEDED"
    prompt = Upstream.seen[0]["body"]["messages"][0]["content"]
    assert FACT in prompt
    assert SECRET not in prompt


def test_empty_and_invalid_source_lists_are_explicit(client, role_urls) -> None:
    empty, empty_status, _events, _artifact = _run(client, role_urls, allowed=[])
    assert empty["stop_reason"] == "allowed_sources_empty"
    assert empty_status["status"] == "NEEDS_REVIEW"


def test_missing_key_is_needs_review_and_does_not_call_upstream(client, role_urls) -> None:
    result, status, events, _artifact = _run(client, role_urls, key="")
    assert result["stop_reason"] == "live_provider_unconfigured"
    assert status["status"] == "NEEDS_REVIEW"
    payload = next(row["payload"] for row in events if row["event_type"] == "task.needs_review")
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["missing"] == ["AETHER_LIVE_MODEL_KEY"]
    assert KEY not in json.dumps(payload)
    assert Upstream.seen == []


def test_upstream_error_is_not_success(client, role_urls) -> None:
    failed, failed_status, failed_events, _artifact = _run(client, role_urls, mode="error")
    assert failed["status"] != "SUCCEEDED"
    assert failed_status["status"] != "SUCCEEDED"
    assert any(row["event_type"] == "task.needs_review" for row in failed_events)


def test_upstream_timeout_is_not_success(client, role_urls) -> None:
    timed, timed_status, timed_events, _artifact = _run(client, role_urls, timeout=0.2, delay=1.0)
    assert timed["status"] != "SUCCEEDED"
    assert timed_status["status"] != "SUCCEEDED"
    assert any(row["event_type"] == "task.needs_review" for row in timed_events)


def test_invalid_source_list_is_explicit(client, role_urls) -> None:
    invalid, invalid_status, _events, _artifact = _run(client, role_urls, allowed=[str(uuid.uuid4())])
    assert invalid["stop_reason"] == "allowed_sources_invalid"
    assert invalid_status["status"] == "NEEDS_REVIEW"
