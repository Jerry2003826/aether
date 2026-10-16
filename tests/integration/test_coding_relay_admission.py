"""Coding relay spends one child ticket per call. Replay and over-budget stop before the provider."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text

from aether_gateway.model import CODING_CALL_OUTPUT_CAP, GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from tests.integration.sysfix_b_helpers import project

_KEY = "gateway-provider-key"


class _Upstream(BaseHTTPRequestHandler):
    bodies: list[dict]

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length)
        _Upstream.bodies.append(json.loads(raw.decode()))
        payload = json.dumps(
            {
                "id": "chatcmpl-coding",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_replayed_ticket_and_over_budget_are_rejected_before_the_provider(client, role_urls) -> None:
    _Upstream.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    owner_id, project_id, _headers = project(client)
    database = Database(role_urls["aether_worker"])
    workflow = f"coding-admit-{uuid.uuid4()}"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_id = conn.execute(
            text(
                "INSERT INTO tasks (project_id, original_goal, workflow_id, status) "
                "VALUES (:project_id, '编码网关', :workflow, 'RUNNING') RETURNING id::text"
            ),
            {"project_id": project_id, "workflow": workflow},
        ).scalar_one()
    ledger = BudgetLedger(database)
    ledger.open_account(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        limit_micro_usd=100_000,
        token_limit=1_000,
        max_requests=4,
    )
    request_key = f"coding-admit-{uuid.uuid4().hex}"
    attempt = uuid.uuid4().hex
    reserved = ledger.reserve(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
        amount=4096,
        role="coding",
        tokens=CODING_CALL_OUTPUT_CAP,
        model_alias="coder",
    )
    assert reserved is True
    gateway = ModelGateway(
        database,
        upstream_base=f"http://127.0.0.1:{server.server_address[1]}/v1",
        provider_key=_KEY,
        timeout=5,
    )
    token = gateway.issue_token(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
        attempt_id=attempt,
        capabilities=["coding:model"],
        aliases=["coder"],
    )
    first = {
        "model": "openai/coder",
        "messages": [{"role": "user", "content": "one"}],
        "timeout": 1e12,
        "stream": True,
        "user": "root",
        "max_tokens": 99999,
    }
    try:
        gateway.relay_chat(token, first)
        with pytest.raises(GatewayDenied) as replayed:
            gateway.relay_chat(token, dict(first))
        assert replayed.value.code == "token_rejected"
        assert replayed.value.sent is False
        with pytest.raises(GatewayDenied) as overrun:
            gateway.relay_chat(token, {"model": "coder", "messages": [{"role": "user", "content": "two"}]})
        assert overrun.value.code == "budget_exceeded"
        assert overrun.value.sent is False
    finally:
        server.shutdown()
        server.server_close()
    assert len(_Upstream.bodies) == 1
    forwarded = _Upstream.bodies[0]
    assert forwarded["max_tokens"] == CODING_CALL_OUTPUT_CAP
    assert "timeout" not in forwarded
    assert "stream" not in forwarded
    assert "user" not in forwarded
    assert "aether_job_token" not in forwarded
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        requests = conn.execute(
            text(
                "SELECT role, usage_status, actual_micro_usd, usage_detail, request_key "
                "FROM model_requests WHERE project_id = :project_id AND task_id = :task_id ORDER BY role"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().all()
        account = conn.execute(
            text(
                "SELECT held_micro_usd, requests_admitted, tokens_held FROM budget_accounts "
                "WHERE project_id = :project_id AND task_id = :task_id"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().one()
        grants = conn.execute(
            text(
                "SELECT revoked_at IS NULL AS live, scope->>'request_key' AS request_key "
                "FROM capability_grants WHERE project_id = :project_id AND task_id = :task_id"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().all()
    by_role = {row["role"]: row for row in requests}
    assert set(by_role) == {"coding", "coding_call"}
    assert by_role["coding"]["usage_status"] == "reserved"
    detail = by_role["coding"]["usage_detail"]
    assert detail["measured_input_tokens"] == 3
    assert detail["measured_output_tokens"] == 2
    assert detail["pending_tokens"] == 0
    assert by_role["coding_call"]["usage_status"] == "known"
    assert int(by_role["coding_call"]["actual_micro_usd"]) == 0
    assert int(account["held_micro_usd"]) == 4096
    assert int(account["requests_admitted"]) == 1
    assert int(account["tokens_held"]) == CODING_CALL_OUTPUT_CAP
    live = [row["request_key"] for row in grants if row["live"]]
    revoked = [row["request_key"] for row in grants if not row["live"]]
    assert live == [request_key]
    assert len(revoked) == 1
    assert revoked[0].startswith(request_key + ":call:")
