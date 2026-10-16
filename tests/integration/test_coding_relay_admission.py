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
    fencing = 1
    binding = _leased_job(database, owner_id=owner_id, project_id=project_id, task_id=task_id, fencing=fencing)
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
        attempt_id=str(fencing),
        capabilities=["coding:model"],
        aliases=["coder"],
        job_id=binding["job_id"],
        operation_id=binding["operation_id"],
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
                "FROM capability_grants WHERE project_id = :project_id AND task_id = :task_id AND capability_type = 'model'"
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


def test_coding_lease_does_not_rewrite_task_status(client, role_urls) -> None:
    _Upstream.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    owner_id, project_id, _headers = project(client)
    database = Database(role_urls["aether_worker"])
    gateway = ModelGateway(
        database,
        upstream_base=f"http://127.0.0.1:{server.server_address[1]}/v1",
        provider_key=_KEY,
        timeout=5,
    )
    try:
        for status in ("WAITING_APPROVAL", "FAILED", "SUCCEEDED", "CANCELLED"):
            _call_on_task(database, gateway, owner_id=owner_id, project_id=project_id, status=status, fencing=4, lease_seconds=300)
        stale = _prepare_task(database, owner_id=owner_id, project_id=project_id, status="WAITING_APPROVAL", fencing=7, lease_seconds=300)
        stale_token = _session_token(gateway, owner_id=owner_id, project_id=project_id, binding=stale, fencing=6)
        with pytest.raises(GatewayDenied) as mismatch:
            gateway.relay_chat(stale_token, {"model": "coder", "messages": [{"role": "user", "content": "stale"}]})
        assert mismatch.value.code == "token_rejected"
        assert mismatch.value.sent is False
        assert _task_status(database, owner_id, project_id, stale["task_id"]) == "WAITING_APPROVAL"
        expired = _prepare_task(database, owner_id=owner_id, project_id=project_id, status="SUCCEEDED", fencing=2, lease_seconds=-60)
        expired_token = _session_token(gateway, owner_id=owner_id, project_id=project_id, binding=expired, fencing=2)
        with pytest.raises(GatewayDenied) as late:
            gateway.relay_chat(expired_token, {"model": "coder", "messages": [{"role": "user", "content": "expired"}]})
        assert late.value.code == "token_rejected"
        assert late.value.sent is False
        assert _task_status(database, owner_id, project_id, expired["task_id"]) == "SUCCEEDED"
    finally:
        server.shutdown()
        server.server_close()
    assert len(_Upstream.bodies) == 4


def _call_on_task(
    database: Database,
    gateway: ModelGateway,
    *,
    owner_id: str,
    project_id: str,
    status: str,
    fencing: int,
    lease_seconds: int,
) -> None:
    binding = _prepare_task(
        database,
        owner_id=owner_id,
        project_id=project_id,
        status=status,
        fencing=fencing,
        lease_seconds=lease_seconds,
    )
    token = _session_token(gateway, owner_id=owner_id, project_id=project_id, binding=binding, fencing=fencing)
    gateway.relay_chat(token, {"model": "coder", "messages": [{"role": "user", "content": status}]})
    assert _task_status(database, owner_id, project_id, binding["task_id"]) == status


def _prepare_task(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    status: str,
    fencing: int,
    lease_seconds: int,
) -> dict[str, str]:
    workflow = f"coding-status-{uuid.uuid4()}"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_id = conn.execute(
            text(
                "INSERT INTO tasks (project_id, original_goal, workflow_id, status) "
                "VALUES (:project_id, '编码状态', :workflow, :status) RETURNING id::text"
            ),
            {"project_id": project_id, "workflow": workflow, "status": status},
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
    request_key = f"coding-status-{uuid.uuid4().hex}"
    assert ledger.reserve(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
        amount=4096,
        role="coding",
        tokens=CODING_CALL_OUTPUT_CAP,
        model_alias="coder",
    )
    job = _leased_job(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        fencing=fencing,
        lease_seconds=lease_seconds,
    )
    return {"task_id": task_id, "request_key": request_key, **job}


def _session_token(
    gateway: ModelGateway,
    *,
    owner_id: str,
    project_id: str,
    binding: dict[str, str],
    fencing: int,
) -> str:
    return gateway.issue_token(
        owner_id=owner_id,
        project_id=project_id,
        task_id=binding["task_id"],
        request_key=binding["request_key"],
        attempt_id=str(fencing),
        capabilities=["coding:model"],
        aliases=["coder"],
        job_id=binding["job_id"],
        operation_id=binding["operation_id"],
    )


def _task_status(database: Database, owner_id: str, project_id: str, task_id: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        status = conn.execute(
            text("SELECT status FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).scalar_one()
    return str(status)


def _leased_job(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    fencing: int,
    lease_seconds: int = 300,
) -> dict[str, str]:
    job_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    capability_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    digest = "0" * 64
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO artifacts (id, project_id, task_id, sha256, byte_size, media_type, storage_key, classification) "
                "VALUES (:id, :project_id, :task_id, :sha, 1, 'application/json', :key, 'internal')"
            ),
            {
                "id": artifact_id,
                "project_id": project_id,
                "task_id": task_id,
                "sha": digest,
                "key": f"coding-lease-{job_id}",
            },
        )
        conn.execute(
            text(
                "INSERT INTO capability_grants (id, project_id, task_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
                "VALUES (:id, :project_id, :task_id, 'aether_worker', :token_hash, 'coding:execute', CAST(:scope AS jsonb), now() + interval '1 hour', 'v1')"
            ),
            {
                "id": capability_id,
                "project_id": project_id,
                "task_id": task_id,
                "token_hash": uuid.uuid4().hex,
                "scope": json.dumps({"project_id": project_id}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO operations (id, project_id, task_id, intent_key, tool_id, args_sha256, intent_digest, normalized_arguments, "
                "resource_id, resource_version, idempotency_class, risk, capability_id, state, fencing_token, lease_until) "
                "VALUES (:id, :project_id, :task_id, :intent_key, 'coding.start', :digest, :digest, CAST(:arguments AS jsonb), "
                ":resource_id, '1', 'idempotent_write', 'R1', :capability_id, 'DISPATCHED', :fencing, now() + make_interval(secs => :lease_seconds))"
            ),
            {
                "id": operation_id,
                "project_id": project_id,
                "task_id": task_id,
                "intent_key": f"{job_id}:{fencing}",
                "digest": digest,
                "arguments": json.dumps({"fencing_token": fencing}),
                "resource_id": artifact_id,
                "capability_id": capability_id,
                "fencing": fencing,
                "lease_seconds": lease_seconds,
            },
        )
        conn.execute(
            text(
                "INSERT INTO coding_jobs (id, project_id, task_id, operation_id, job_key, state, base_artifact_id, deadline, fencing_token) "
                "VALUES (:id, :project_id, :task_id, :operation_id, :job_key, 'ACTIVE', :artifact_id, now() + interval '1 hour', :fencing)"
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "task_id": task_id,
                "operation_id": operation_id,
                "job_key": f"lease-{job_id}",
                "artifact_id": artifact_id,
                "fencing": fencing,
            },
        )
    return {"job_id": job_id, "operation_id": operation_id}
