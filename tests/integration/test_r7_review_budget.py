"""P2-09: review exit, owner approval, active time, workflow delivery, and evidence.

These checks use the real API role, worker role, PostgreSQL, and Temporal.
They describe the behavior in docs/04 and the system-round findings S-010, S-015,
S-016, S-019, S-032, S-033, S-043, and S-071.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_domain.security import sha256_text
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_storage.operations import OperationLedger
from aether_worker.activities import record_applied
from aether_worker.dispatcher import deliver_pending
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import apply_task_control, configure, execute_task, materialize_goal_effect, prepare_task
from aether_worker.outbox import enqueue_task, insert_followup_command
from aether_worker.workflows import TaskWorkflow
from tests.integration.system_audit import FACT, FACT_GOAL, endpoints, key, project, store_for, worker_db
from tests.integration.test_auth import ORIGIN, _bootstrap
from tests.integration.test_workflow import _UnavailableTemporal


def _temporal_address() -> str:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    return address


def _headers(client) -> tuple[str, str, dict[str, str]]:
    return project(client)


def _task(database: Database, owner_id: str, project_id: str, goal: str, *, budget: dict | None = None, allowed: list[str] | None = None) -> dict[str, str]:
    created = enqueue_task(
        database,
        owner_id=owner_id,
        project_id=project_id,
        goal=goal,
        command_key=str(uuid.uuid4()),
        budget=budget,
        allowed_source_ids=allowed,
    )
    return {"project_id": project_id, "task_id": created["task_id"], "command_id": created["command_key"], "workflow_id": created["workflow_id"]}


def _approve(database: Database, owner_id: str, project_id: str, task_id: str, workflow_id: str) -> str:
    command_key = str(uuid.uuid4())
    payload = {"task_id": task_id, "workflow_id": workflow_id, "command_id": command_key, "kind": "approve_goal", "owner_id": owner_id}
    digest = sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO command_outbox (project_id, task_id, command_key, kind, payload, payload_sha256) "
                "VALUES (:project_id, :task_id, :command_key, 'approve_goal', CAST(:payload AS jsonb), :digest)"
            ),
            {"project_id": project_id, "task_id": task_id, "command_key": command_key, "payload": json.dumps(payload), "digest": digest},
        )
    return command_key


def _status(database: Database, owner_id: str, project_id: str, task_id: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return str(conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one())


def _contracts(database: Database, owner_id: str, project_id: str, task_id: str) -> int:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return int(conn.execute(text("SELECT count(*) FROM goal_contracts WHERE task_id = :id"), {"id": task_id}).scalar_one())


def test_worker_does_not_approve_the_contract_as_the_owner(client, role_urls) -> None:
    owner_id, project_id, session_headers = _headers(client)
    database = worker_db(role_urls)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**session_headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = _task(database, owner_id, project_id, FACT_GOAL)
    scripted = endpoints(role_urls, store_for(client, role_urls).root, provider="scripted")
    result = materialize_goal_effect(scripted, payload)
    assert result["status"] == "waiting_approval", result
    assert _status(database, owner_id, project_id, payload["task_id"]) == "WAITING_APPROVAL"
    assert _contracts(database, owner_id, project_id, payload["task_id"]) == 0


def test_explicit_owner_approval_then_runs_the_task(client, role_urls) -> None:
    owner_id, project_id, headers = _headers(client)
    database = worker_db(role_urls)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = _task(database, owner_id, project_id, FACT_GOAL)
    scripted = endpoints(role_urls, store_for(client, role_urls).root, provider="scripted")
    _approve(database, owner_id, project_id, payload["task_id"], payload["workflow_id"])
    result = materialize_goal_effect(scripted, payload)
    assert result["status"] == "SUCCEEDED", result
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        approved_by = conn.execute(text("SELECT approved_by::text FROM goal_contracts WHERE task_id = :id"), {"id": payload["task_id"]}).scalar_one()
    assert approved_by == owner_id


def test_needs_review_approve_returns_to_the_original_stage_and_reject_cancels(client, role_urls) -> None:
    owner_id, project_id, headers = _headers(client)
    database = worker_db(role_urls)
    scripted = endpoints(role_urls, store_for(client, role_urls).root, provider="scripted")
    waiting = _task(database, owner_id, project_id, FACT_GOAL)
    _approve(database, owner_id, project_id, waiting["task_id"], waiting["workflow_id"])
    reviewed = materialize_goal_effect(scripted, waiting)
    assert reviewed["status"] == "NEEDS_REVIEW", reviewed
    listed = client.get(f"/v1/projects/{project_id}/approvals")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1, items
    approval = items[0]
    assert approval["normalized_payload"]["action"] == "resolve_review"
    assert approval["normalized_payload"]["from_status"] in {"QUEUED", "RUNNING", "VERIFYING"}
    origin = approval["normalized_payload"]["from_status"]
    decided = client.post(
        f"/v1/projects/{project_id}/approvals/{approval['id']}/decision",
        headers={**headers, "Idempotency-Key": key(), "If-Match": str(approval["version"])},
        json={"decision": "approve", "expected_intent_hash": approval["intent_digest"]},
    )
    assert decided.status_code == 202, decided.text
    configure(scripted)
    asyncio.run(_deliver_until_returned(role_urls, scripted, waiting["task_id"], owner_id, project_id, origin))
    assert _status(database, owner_id, project_id, waiting["task_id"]) != "CANCELLED"

    rejected_task = _task(database, owner_id, project_id, "没有资料的另一个目标")
    _approve(database, owner_id, project_id, rejected_task["task_id"], rejected_task["workflow_id"])
    again = materialize_goal_effect(scripted, rejected_task)
    assert again["status"] == "NEEDS_REVIEW", again
    pending = client.get(f"/v1/projects/{project_id}/approvals").json()["items"]
    match = [item for item in pending if item["task_id"] == rejected_task["task_id"]]
    assert len(match) == 1, pending
    refusal = client.post(
        f"/v1/projects/{project_id}/approvals/{match[0]['id']}/decision",
        headers={**headers, "Idempotency-Key": key(), "If-Match": str(match[0]["version"])},
        json={"decision": "reject", "expected_intent_hash": match[0]["intent_digest"]},
    )
    assert refusal.status_code == 202, refusal.text
    asyncio.run(_deliver_until_status(role_urls, scripted, rejected_task["task_id"], owner_id, project_id, "CANCELLED"))
    assert _status(database, owner_id, project_id, rejected_task["task_id"]) == "CANCELLED"


def test_queue_and_pause_do_not_consume_active_seconds(client, role_urls) -> None:
    owner_id, project_id, headers = _headers(client)
    database = worker_db(role_urls)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    budget = {"max_cost_micro_usd": 2_000_000, "max_tokens": 100_000, "max_model_requests": 4, "active_seconds": 60, "verification_reserve_fraction": 0.2}
    payload = _task(database, owner_id, project_id, FACT_GOAL, budget=budget)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE tasks SET created_at = :created WHERE id = :id"),
            {"created": datetime.now(UTC) - timedelta(hours=3), "id": payload["task_id"]},
        )
    scripted = endpoints(role_urls, store_for(client, role_urls).root, provider="scripted")
    _approve(database, owner_id, project_id, payload["task_id"], payload["workflow_id"])
    queued = materialize_goal_effect(scripted, payload)
    assert queued.get("stop_reason") != "budget_exhausted", queued
    assert queued["status"] == "SUCCEEDED", queued

    limited = _task(
        database,
        owner_id,
        project_id,
        FACT_GOAL,
        budget={"max_cost_micro_usd": 2_000_000, "max_tokens": 100_000, "max_model_requests": 4, "active_seconds": 5, "verification_reserve_fraction": 0.2},
    )
    _approve(database, owner_id, project_id, limited["task_id"], limited["workflow_id"])
    configure(scripted)
    assert asyncio.run(prepare_task({"project_id": project_id, "task_id": limited["task_id"], "command_id": limited["command_id"]}))["status"] == "prepared"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        updated = conn.execute(
            text(
                "UPDATE task_events SET created_at = now() - interval '30 seconds' "
                "WHERE task_id = :id AND event_type = 'task.active' AND payload->>'phase' = 'start'"
            ),
            {"id": limited["task_id"]},
        ).rowcount
    assert updated == 1
    finished = asyncio.run(execute_task({"project_id": project_id, "task_id": limited["task_id"], "command_id": limited["command_id"]}))
    assert finished.get("stop_reason") == "budget_exhausted", finished
    assert finished["status"] != "SUCCEEDED"


def test_workflow_ends_on_terminal_status_and_can_continue_as_new(client, role_urls) -> None:
    address = _temporal_address()
    owner_id, project_id, headers = _headers(client)
    database = worker_db(role_urls)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = _task(database, owner_id, project_id, FACT_GOAL)
    scripted = endpoints(role_urls, store_for(client, role_urls).root, provider="scripted")
    _approve(database, owner_id, project_id, payload["task_id"], payload["workflow_id"])
    configure(scripted)
    applied = asyncio.run(_run_until_terminal(address, scripted.task_queue, payload))
    assert payload["command_id"] in applied
    assert _status(database, owner_id, project_id, payload["task_id"]) == "SUCCEEDED"

    paused = _task(database, owner_id, project_id, "只用来观察 continue-as-new")
    pause_key = str(uuid.uuid4())
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        insert_followup_command(
            conn,
            project_id=project_id,
            task_id=paused["task_id"],
            workflow_id=paused["workflow_id"],
            command_key=pause_key,
            kind="pause",
        )
    continued = asyncio.run(_continue_once(address, paused, pause_key))
    assert pause_key in continued


def test_completed_workflow_is_not_started_again(client, api_db, role_urls) -> None:
    address = _temporal_address()
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    created_project = client.post(
        "/v1/projects",
        headers={**ORIGIN, "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": key()},
        json={"name": "复用策略"},
    )
    assert created_project.status_code == 201, created_project.text
    project_id = created_project.json()["id"]
    created = enqueue_task(api_db, owner_id=session["owner_id"], project_id=project_id, goal="结束", command_key="reuse-start")
    dispatcher = Database(role_urls["aether_dispatcher"])
    queue = f"reuse-{uuid.uuid4()}"

    async def run() -> tuple[str, str]:
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[TaskWorkflow], activities=[record_applied]):
            await deliver_pending(dispatcher, temporal, task_queue=queue)
            handle = temporal.get_workflow_handle(created["workflow_id"])
            first_run = (await handle.describe()).run_id
            await handle.signal(TaskWorkflow.close)
            await handle.result()
        with api_db.transaction(owner_id=session["owner_id"], project_id=project_id) as conn:
            insert_followup_command(
                conn,
                project_id=project_id,
                task_id=created["task_id"],
                workflow_id=created["workflow_id"],
                command_key="reuse-again",
                kind="start",
            )
        async with Worker(temporal, task_queue=queue, workflows=[TaskWorkflow], activities=[record_applied]):
            await deliver_pending(dispatcher, temporal, task_queue=queue)
            handle = temporal.get_workflow_handle(created["workflow_id"])
            description = await handle.describe()
            return first_run, f"{description.run_id}:{description.status.name}"

    first_run, later = asyncio.run(run())
    later_run, status_name = later.split(":", 1)
    assert later_run == first_run
    assert status_name == "COMPLETED"
    with api_db.transaction(owner_id=session["owner_id"], project_id=project_id) as conn:
        again = conn.execute(text("SELECT status FROM command_outbox WHERE command_key = 'reuse-again'")).scalar_one()
    assert again == "DELIVERED"


def test_dispatcher_lease_backs_off_and_stops_at_eight(client, api_db, role_urls, aether_admin) -> None:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    created_project = client.post(
        "/v1/projects",
        headers={**ORIGIN, "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": key()},
        json={"name": "投递租约"},
    )
    assert created_project.status_code == 201, created_project.text
    project_id = created_project.json()["id"]
    enqueue_task(api_db, owner_id=session["owner_id"], project_id=project_id, goal="租约", command_key="lease-start")
    dispatcher = Database(role_urls["aether_dispatcher"])
    first = asyncio.run(deliver_pending(dispatcher, _UnavailableTemporal(), task_queue="lease"))  # type: ignore[arg-type]
    assert first["failed"] == ["lease-start"]
    with psycopg.connect(aether_admin) as conn:
        row = conn.execute("SELECT status, attempts, lease_until FROM command_outbox WHERE command_key = 'lease-start'").fetchone()
    assert row is not None
    assert row[0] == "DELIVERING"
    assert int(row[1]) == 1
    assert row[2] is not None and row[2] > datetime.now(UTC)
    second = asyncio.run(deliver_pending(dispatcher, _UnavailableTemporal(), task_queue="lease"))  # type: ignore[arg-type]
    assert second == {"delivered": [], "failed": []}
    with psycopg.connect(aether_admin) as conn:
        attempts = conn.execute("SELECT attempts FROM command_outbox WHERE command_key = 'lease-start'").fetchone()
    assert attempts is not None and int(attempts[0]) == 1
    for _ in range(7):
        with psycopg.connect(aether_admin) as conn:
            conn.execute("UPDATE command_outbox SET lease_until = now() - interval '1 second' WHERE command_key = 'lease-start'")
            conn.commit()
        asyncio.run(deliver_pending(dispatcher, _UnavailableTemporal(), task_queue="lease"))  # type: ignore[arg-type]
    with psycopg.connect(aether_admin) as conn:
        final = conn.execute("SELECT status, attempts FROM command_outbox WHERE command_key = 'lease-start'").fetchone()
    assert final is not None
    assert int(final[1]) == 8
    assert final[0] == "FAILED"


def test_failed_approval_decision_does_not_stick_the_idempotency_key(client, api_db, role_urls) -> None:
    owner_id, project_id, headers = _headers(client)
    database = worker_db(role_urls)
    payload = _task(database, owner_id, project_id, "审批幂等")
    intent = {"intent_key": "decide-1", "tool_id": "example.write", "arguments": {"title": "草稿"}, "resource_id": "resource-1", "resource_version": "v1", "risk": "R2"}
    digest = sha256_text(json.dumps(intent, sort_keys=True, separators=(",", ":")))
    approval_id = OperationLedger(database).request_approval(
        owner_id=owner_id,
        project_id=project_id,
        task_id=payload["task_id"],
        intent=intent,
        risk="R2",
    )
    body = {"decision": "approve", "expected_intent_hash": "ab" * 32}
    first = client.post(
        f"/v1/projects/{project_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "decide-stuck-key", "If-Match": "1"},
        json=body,
    )
    assert first.status_code == 409, first.text
    assert first.json()["code"] == "approval_rejected"
    second = client.post(
        f"/v1/projects/{project_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "decide-stuck-key", "If-Match": "1"},
        json=body,
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "approval_rejected"
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        stored = conn.execute(
            text("SELECT response_status FROM idempotency_keys WHERE idempotency_key = 'decide-stuck-key'")
        ).scalar_one()
    assert int(stored) == 409


def test_overrun_reconciliation_confirms_or_releases_the_hold(client, role_urls) -> None:
    owner_id, project_id, _session = _headers(client)
    database = worker_db(role_urls)
    payload = _task(database, owner_id, project_id, "超额对账")
    ledger = BudgetLedger(database)
    ledger.open_account(owner_id=owner_id, project_id=project_id, task_id=payload["task_id"], limit_micro_usd=2_000_000, token_limit=1000, max_requests=4, verification_reserve_fraction=0, active_seconds=1800)
    prepared = ledger.prepare_model_call(
        owner_id=owner_id,
        project_id=project_id,
        task_id=payload["task_id"],
        request_key="research-over",
        amount=1000,
        role="research",
        tokens=10,
        model_alias="audit-model",
        prompt_sha256="ab" * 32,
        output_cap=4,
        source_snapshot_sha256="cd" * 32,
        attempt_id=uuid.uuid4().hex,
    )
    assert prepared["action"] == "send"
    booked = ledger.record_model_response(
        owner_id=owner_id,
        project_id=project_id,
        task_id=payload["task_id"],
        request_key="research-over",
        output="超出",
        input_tokens=2,
        output_tokens=2,
        cost_micro_usd=5000,
        upstream_model="audit-model",
        requested_alias="audit-model",
        cost_source="provider",
        pricing_version="v1",
    )
    assert booked == "overrun"
    confirmed = ledger.reconcile_overrun(owner_id=owner_id, project_id=project_id, task_id=payload["task_id"], request_key="research-over", decision="confirm")
    assert confirmed == "confirmed"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        account = conn.execute(text("SELECT held_micro_usd, spent_micro_usd FROM budget_accounts WHERE task_id = :id"), {"id": payload["task_id"]}).one()
    assert int(account.held_micro_usd) == 0
    assert int(account.spent_micro_usd) == 5000
    again = ledger.reconcile_overrun(owner_id=owner_id, project_id=project_id, task_id=payload["task_id"], request_key="research-over", decision="confirm")
    assert again == "confirmed"

    released_task = _task(database, owner_id, project_id, "释放超额")
    ledger.open_account(owner_id=owner_id, project_id=project_id, task_id=released_task["task_id"], limit_micro_usd=2_000_000, token_limit=1000, max_requests=4, verification_reserve_fraction=0, active_seconds=1800)
    ledger.prepare_model_call(
        owner_id=owner_id,
        project_id=project_id,
        task_id=released_task["task_id"],
        request_key="research-release",
        amount=1000,
        role="research",
        tokens=10,
        model_alias="audit-model",
        prompt_sha256="ef" * 32,
        output_cap=4,
        source_snapshot_sha256="11" * 32,
        attempt_id=uuid.uuid4().hex,
    )
    assert ledger.record_model_response(
        owner_id=owner_id,
        project_id=project_id,
        task_id=released_task["task_id"],
        request_key="research-release",
        output="超出",
        input_tokens=2,
        output_tokens=2,
        cost_micro_usd=5000,
        upstream_model="audit-model",
        requested_alias="audit-model",
        cost_source="provider",
        pricing_version="v1",
    ) == "overrun"
    assert ledger.reconcile_overrun(owner_id=owner_id, project_id=project_id, task_id=released_task["task_id"], request_key="research-release", decision="release") == "released"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        released = conn.execute(text("SELECT held_micro_usd, spent_micro_usd FROM budget_accounts WHERE task_id = :id"), {"id": released_task["task_id"]}).one()
    assert int(released.held_micro_usd) == 0
    assert int(released.spent_micro_usd) == 0


def test_succeeded_answer_and_report_stay_after_a_delete_attempt(client, role_urls) -> None:
    owner_id, project_id, headers = _headers(client)
    database = worker_db(role_urls)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", f"{FACT}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    source_id = uploaded.json()["artifact_id"]
    payload = _task(database, owner_id, project_id, FACT_GOAL)
    scripted = endpoints(role_urls, store_for(client, role_urls).root, provider="scripted")
    _approve(database, owner_id, project_id, payload["task_id"], payload["workflow_id"])
    result = materialize_goal_effect(scripted, payload)
    assert result["status"] == "SUCCEEDED", result
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        answer = conn.execute(
            text("SELECT artifact_id::text FROM task_artifacts WHERE task_id = :id AND classification = 'task_answer' AND deleted_at IS NULL"),
            {"id": payload["task_id"]},
        ).scalar_one()
        report = conn.execute(
            text("SELECT report_artifact_id::text FROM verification_results WHERE task_id = :id"),
            {"id": payload["task_id"]},
        ).scalar_one()
    denied_answer = client.delete(f"/v1/projects/{project_id}/artifacts/{answer}", headers={**headers, "Idempotency-Key": key()})
    assert denied_answer.status_code == 409, denied_answer.text
    assert denied_answer.json()["code"] == "evidence_retained"
    denied_report = client.delete(f"/v1/projects/{project_id}/artifacts/{report}", headers={**headers, "Idempotency-Key": key()})
    assert denied_report.status_code == 409, denied_report.text
    assert denied_report.json()["code"] == "evidence_retained"
    removed_source = client.delete(f"/v1/projects/{project_id}/artifacts/{source_id}", headers={**headers, "Idempotency-Key": key()})
    assert removed_source.status_code == 204, removed_source.text
    task = client.get(f"/v1/projects/{project_id}/tasks/{payload['task_id']}")
    assert task.status_code == 200, task.text
    assert task.json()["status"] == "SUCCEEDED"


async def _deliver_until_status(role_urls: dict[str, str], scripted, task_id: str, owner_id: str, project_id: str, expected: str) -> None:
    address = _temporal_address()
    temporal = await Client.connect(address)
    dispatcher = Database(role_urls["aether_dispatcher"])
    database = worker_db(role_urls)
    async with Worker(temporal, task_queue=scripted.task_queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task, apply_task_control]):
        await deliver_pending(dispatcher, temporal, task_queue=scripted.task_queue, workflow=DurableTaskWorkflow)
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if _status(database, owner_id, project_id, task_id) == expected:
                return
            await asyncio.sleep(0.2)
        raise AssertionError(_status(database, owner_id, project_id, task_id))


async def _deliver_until_returned(role_urls: dict[str, str], scripted, task_id: str, owner_id: str, project_id: str, origin: str) -> None:
    address = _temporal_address()
    temporal = await Client.connect(address)
    dispatcher = Database(role_urls["aether_dispatcher"])
    database = worker_db(role_urls)
    async with Worker(temporal, task_queue=scripted.task_queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task, apply_task_control]):
        await deliver_pending(dispatcher, temporal, task_queue=scripted.task_queue, workflow=DurableTaskWorkflow)
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
                stages = [
                    str(payload.get("status") or "")
                    for payload in conn.execute(
                        text("SELECT payload FROM task_events WHERE task_id = :id AND event_type = 'task.status' ORDER BY sequence"),
                        {"id": task_id},
                    ).scalars()
                ]
            if "NEEDS_REVIEW" in stages and origin in stages[stages.index("NEEDS_REVIEW") + 1 :]:
                return
            await asyncio.sleep(0.2)
        raise AssertionError("review did not return to the original stage")


async def _run_until_terminal(address: str, queue: str, payload: dict[str, str]) -> list[str]:
    temporal = await Client.connect(address)
    async with Worker(temporal, task_queue=queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task, apply_task_control]):
        handle = await temporal.start_workflow(
            DurableTaskWorkflow.run,
            {"task_id": payload["task_id"], "intake_pause_seconds": 0},
            id=payload["workflow_id"],
            task_queue=queue,
        )
        await handle.signal(DurableTaskWorkflow.command, {"command_id": payload["command_id"], "project_id": payload["project_id"], "kind": "start"})
        return list(await asyncio.wait_for(handle.result(), timeout=20))


async def _continue_once(address: str, payload: dict[str, str], pause_key: str) -> list[str]:
    temporal = await Client.connect(address)
    queue = f"continue-{uuid.uuid4()}"
    async with Worker(temporal, task_queue=queue, workflows=[DurableTaskWorkflow], activities=[prepare_task, execute_task, apply_task_control]):
        handle = await temporal.start_workflow(
            DurableTaskWorkflow.run,
            {"task_id": payload["task_id"], "intake_pause_seconds": 0, "history_event_limit": 1},
            id=payload["workflow_id"],
            task_queue=queue,
        )
        first = (await handle.describe()).run_id
        await handle.signal(DurableTaskWorkflow.command, {"command_id": pause_key, "project_id": payload["project_id"], "kind": "pause"})
        for _ in range(40):
            current = (await handle.describe()).run_id
            if current != first:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("workflow did not continue as new")
        await handle.signal(DurableTaskWorkflow.close)
        return list(await asyncio.wait_for(handle.result(), timeout=20))
