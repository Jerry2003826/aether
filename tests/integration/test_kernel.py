import asyncio
import json
import os
import threading
import uuid
from pathlib import Path
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_domain.security import sha256_text
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_storage.operations import OperationLedger
from aether_worker.outbox import enqueue_task
from aether_worker.research import EFFECTS, record_research_effect, record_verification, scripted_research
from aether_worker.research_workflow import ResearchWorkflow
from aether_worker.dispatcher import deliver_pending
from tests.integration.test_auth import ORIGIN, _bootstrap
from tests.integration.test_operations import _digest, _intent, _ledger


class _Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length)
        _Upstream.seen.append(json.loads(raw.decode()))
        payload = json.dumps(
            {
                "id": "chatcmpl-scripted",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "scripted-candidate"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def _project(client):
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    project = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "kernel-project"}, json={"name": "闭环"})
    assert project.status_code == 201, project.text
    return session["owner_id"], project.json()["id"], headers


def test_scripted_loop_records_hash_and_gateway_calls(client, api_db, role_urls, aether_admin, tmp_path) -> None:
    script = Path("migrations/sql/0004_gateway_lookup.sql").read_text(encoding="utf-8")
    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute(script)
    owner_id, project_id, _headers = _project(client)
    document = "本地笔记：甲的原文".encode()
    store = ArtifactStore(tmp_path / "artifacts", api_db)
    stored = store.put(owner_id=owner_id, project_id=project_id, data=document, media_type="text/plain")
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from aether_gateway.model import ModelGateway

        gateway = ModelGateway(Database(role_urls["aether_worker"]), upstream_base=f"http://127.0.0.1:{server.server_address[1]}/v1", provider_key="sk-scripted")
        token = gateway.issue_token(owner_id=owner_id, project_id=project_id, capabilities=["knowledge:read"], aliases=["actor"], egress=["https://example.test"])
        result = scripted_research(store=store, gateway=gateway, token=token, owner_id=owner_id, project_id=project_id, artifact_id=str(stored["artifact_id"]))
    finally:
        server.shutdown()
    assert result["model_mode"] == "scripted"
    assert result["tool_text"] == document.decode()
    assert result["artifact_sha256"] == stored["sha256"]
    assert result["gateway_calls"] == 1
    assert len(_Upstream.seen) == 1
    assert sum(1 for line in gateway.audit if "outcome=" in line) == 1
    assert document.decode() in _Upstream.seen[0]["messages"][0]["content"]
    worker = Database(role_urls["aether_worker"])
    digest = "cd" * 32
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_id = conn.execute(
            text("INSERT INTO tasks (project_id, original_goal, workflow_id, status) VALUES (:project_id, '验收', :workflow, 'QUEUED') RETURNING id::text"),
            {"project_id": project_id, "workflow": f"kernel-{uuid.uuid4()}"},
        ).scalar_one()
        contract_id = conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, '{}'::jsonb, :digest, :owner, now()) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "digest": digest, "owner": owner_id},
        ).scalar_one()
    verified = record_verification(
        Database(role_urls["aether_verifier"]),
        owner_id=owner_id,
        project_id=project_id,
        task_id=str(task_id),
        contract_id=str(contract_id),
        artifact_id=str(stored["artifact_id"]),
        report_artifact_id=str(stored["artifact_id"]),
        criterion_id="notes",
        artifact_sha256=str(stored["sha256"]),
        observed_sha256=str(stored["sha256"]),
        contract_sha256=digest,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        now=datetime.now(UTC),
        actor_verdict="FAIL",
    )
    assert verified["verdict"] == "PASS"
    assert verified["verifier"] == "aether_verifier"
    assert "FAIL" in verified["reason"]


def test_expired_candidate_is_rejected(client, api_db, role_urls) -> None:
    owner_id, project_id, _headers = _project(client)
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    stored = store.put(owner_id=owner_id, project_id=project_id, data=b"expired-note", media_type="text/plain")
    worker = Database(role_urls["aether_worker"])
    digest = "ef" * 32
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_id = conn.execute(
            text("INSERT INTO tasks (project_id, original_goal, workflow_id) VALUES (:project_id, '过期', :workflow) RETURNING id::text"),
            {"project_id": project_id, "workflow": f"expired-{uuid.uuid4()}"},
        ).scalar_one()
        contract_id = conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, '{}'::jsonb, :digest, :owner, now()) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "digest": digest, "owner": owner_id},
        ).scalar_one()
    verified = record_verification(
        Database(role_urls["aether_verifier"]),
        owner_id=owner_id,
        project_id=project_id,
        task_id=str(task_id),
        contract_id=str(contract_id),
        artifact_id=str(stored["artifact_id"]),
        report_artifact_id=str(stored["artifact_id"]),
        criterion_id="expiry",
        artifact_sha256=str(stored["sha256"]),
        observed_sha256=str(stored["sha256"]),
        contract_sha256=digest,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        now=datetime.now(UTC),
        actor_verdict="PASS",
    )
    assert verified["verdict"] == "FAIL"
    assert verified["verifier"] == "aether_verifier"
    with Database(role_urls["aether_verifier"]).transaction(owner_id=owner_id, project_id=project_id) as conn:
        stored_verdict = conn.execute(text("SELECT verdict FROM verification_results WHERE id = :id"), {"id": verified["id"]}).scalar_one()
    assert stored_verdict == "FAIL"


def test_approval_replay_does_not_repeat_side_effect(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "kernel-replay")
    intent = _intent("kernel-replay-1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id=intent["resource_id"], version="v1")
    calls: list[str] = []

    def sender(payload):
        calls.append(payload["intent_key"])
        return {"resource_version": "v1"}

    first = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="idempotent_write", sender=sender)
    second = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="idempotent_write", sender=sender)
    assert first["state"] == "CONFIRMED"
    assert second["replayed"] is True
    assert calls == ["kernel-replay-1"]


def _effect_count(database: Database, owner_id: str, project_id: str, task_id: str) -> int:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_type = 'research.call'"),
                {"project_id": project_id, "task_id": task_id},
            ).scalar_one()
        )


def test_worker_restart_and_command_redelivery_do_not_repeat(client, api_db, role_urls) -> None:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
            pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
        pytest.skip("AETHER_TEMPORAL_ADDRESS unset; workflow not_run")
    owner_id, project_id, _headers = _project(client)
    created = enqueue_task(api_db, owner_id=owner_id, project_id=project_id, goal="重投", command_key=f"kernel-{uuid.uuid4()}")
    worker = Database(role_urls["aether_worker"])
    dispatcher = Database(role_urls["aether_dispatcher"])
    EFFECTS["database"] = worker
    EFFECTS["owner_id"] = owner_id
    EFFECTS["project_id"] = project_id
    queue = f"aether-kernel-{uuid.uuid4()}"

    async def run() -> list[str]:
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[ResearchWorkflow], activities=[record_research_effect]):
            first = await deliver_pending(dispatcher, temporal, task_queue=queue, workflow=ResearchWorkflow)
            assert first["delivered"] == [created["command_key"]]
            for _ in range(30):
                if _effect_count(worker, owner_id, project_id, created["task_id"]) == 1:
                    break
                await asyncio.sleep(0.1)
            assert _effect_count(worker, owner_id, project_id, created["task_id"]) == 1
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text("UPDATE command_outbox SET status = 'PENDING', delivered_at = NULL WHERE project_id = :project_id AND command_key = :command_key"),
                {"project_id": project_id, "command_key": created["command_key"]},
            )
        async with Worker(temporal, task_queue=queue, workflows=[ResearchWorkflow], activities=[record_research_effect]):
            second = await deliver_pending(dispatcher, temporal, task_queue=queue, workflow=ResearchWorkflow)
            assert second["delivered"] == [created["command_key"]]
            await asyncio.sleep(0.4)
            assert _effect_count(worker, owner_id, project_id, created["task_id"]) == 1
            handle = temporal.get_workflow_handle(created["workflow_id"])
            await handle.signal(ResearchWorkflow.close)
            return list(await handle.result())

    assert asyncio.run(run()) == [created["command_key"]]
