import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_domain.security import sha256_text
from aether_storage.db import Database
from aether_storage.operations import OperationLedger
from aether_worker.activities import record_applied
from aether_worker.dispatcher import deliver_pending
from aether_worker.workflows import TaskWorkflow
from tests.integration.test_auth import ORIGIN, _bootstrap


def _session(client):
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    project = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "control-project"}, json={"name": "控制台"})
    assert project.status_code == 201, project.text
    return session["owner_id"], project.json()["id"], headers


def _task(client, headers, project_id, goal="研究本地笔记", key="task-create-1"):
    created = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**headers, "Idempotency-Key": key},
        json={"goal": goal},
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "accepted"
    return created.json()


def test_create_task_reaches_database_and_temporal_without_fake_success(client, api_db, role_urls) -> None:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
            pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
        pytest.skip("AETHER_TEMPORAL_ADDRESS unset; workflow not_run")
    owner_id, project_id, headers = _session(client)
    accepted = _task(client, headers, project_id)
    replay = _task(client, headers, project_id)
    assert replay == accepted
    conflict = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**headers, "Idempotency-Key": "task-create-1"},
        json={"goal": "另一份目标"},
    )
    assert conflict.status_code == 409
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": accepted["resource_id"]}).scalar_one()
        workflow_id = conn.execute(text("SELECT workflow_id FROM tasks WHERE id = :id"), {"id": accepted["resource_id"]}).scalar_one()
        count = conn.execute(text("SELECT count(*) FROM tasks WHERE project_id = :project_id"), {"project_id": project_id}).scalar_one()
    assert status == "QUEUED"
    assert count == 1
    snapshot = client.get(f"/v1/projects/{project_id}/tasks/{accepted['resource_id']}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["status"] == "QUEUED"
    assert snapshot.json()["budget"]["unknown_request_count"] == 0
    dispatcher = Database(role_urls["aether_dispatcher"])
    queue = f"aether-ui-{uuid.uuid4()}"

    async def deliver() -> list[str]:
        temporal = await Client.connect(address)
        async with Worker(temporal, task_queue=queue, workflows=[TaskWorkflow], activities=[record_applied]):
            result = await deliver_pending(dispatcher, temporal, task_queue=queue)
            assert accepted["command_id"] in result["delivered"]
            handle = temporal.get_workflow_handle(workflow_id)
            applied: list[str] = []
            for _ in range(20):
                applied = list(await handle.query(TaskWorkflow.applied))
                if applied == [accepted["command_id"]]:
                    break
                await asyncio.sleep(0.1)
            return applied

    assert asyncio.run(deliver()) == [accepted["command_id"]]
    again = client.get(f"/v1/projects/{project_id}/tasks/{accepted['resource_id']}")
    assert again.json()["status"] == "QUEUED"


def test_event_replay_shows_pending_command_and_watermark(client, api_db) -> None:
    _owner, project_id, headers = _session(client)
    accepted = _task(client, headers, project_id, key="task-events-1")
    task_id = accepted["resource_id"]
    command_id = str(uuid.uuid4())
    posted = client.post(
        f"/v1/projects/{project_id}/tasks/{task_id}/commands",
        headers={**headers, "Idempotency-Key": "command-pause-1"},
        json={"command_id": command_id, "kind": "pause"},
    )
    assert posted.status_code == 202, posted.text
    assert posted.json()["status"] == "accepted"
    first = client.get(f"/v1/projects/{project_id}/tasks/{task_id}/events")
    assert first.status_code == 200, first.text
    assert "projection_updated_at" in first.text
    assert command_id in first.text
    assert '"sequence": 1' in first.text
    event_id = first.text.split("id: ", 1)[1].split("\n", 1)[0]
    replay = client.get(
        f"/v1/projects/{project_id}/tasks/{task_id}/events",
        headers={"Last-Event-ID": event_id},
    )
    assert replay.status_code == 200, replay.text
    assert '"sequence": 1' not in replay.text
    assert '"sequence": 2' in replay.text
    assert command_id in replay.text
    expired = client.get(
        f"/v1/projects/{project_id}/tasks/{task_id}/events",
        headers={"Last-Event-ID": "evt-missing"},
    )
    assert expired.status_code == 422
    assert expired.json()["code"] == "cursor_expired"
    with api_db.transaction(owner_id=_owner, project_id=project_id) as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    assert status == "QUEUED"


def test_approval_decision_is_authorized_on_the_server(client, api_db, role_urls) -> None:
    owner_id, project_id, headers = _session(client)
    other = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "control-other"}, json={"name": "另一个项目"})
    assert other.status_code == 201, other.text
    accepted = _task(client, headers, project_id, key="task-approval-1")
    intent = {"intent_key": "approve-1", "tool_id": "example.write", "arguments": {"title": "草稿"}, "resource_id": "resource-1", "resource_version": "v1", "risk": "R2"}
    digest = sha256_text(json.dumps(intent, sort_keys=True, separators=(",", ":")))
    ledger = OperationLedger(Database(role_urls["aether_worker"]))
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=accepted["resource_id"], intent=intent, risk="R2")
    hidden = client.get(f"/v1/projects/{other.json()['id']}/approvals")
    assert hidden.status_code == 200
    assert hidden.json()["items"] == []
    visible = client.get(f"/v1/projects/{project_id}/approvals")
    assert [item["id"] for item in visible.json()["items"]] == [approval_id]
    wrong_project = client.post(
        f"/v1/projects/{other.json()['id']}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "decide-other", "If-Match": "1"},
        json={"decision": "approve", "expected_intent_hash": digest},
    )
    assert wrong_project.status_code == 404
    wrong_hash = client.post(
        f"/v1/projects/{project_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "decide-hash", "If-Match": "1"},
        json={"decision": "approve", "expected_intent_hash": "ab" * 32},
    )
    assert wrong_hash.status_code == 409
    stale = client.post(
        f"/v1/projects/{project_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "decide-stale", "If-Match": "9"},
        json={"decision": "approve", "expected_intent_hash": digest},
    )
    assert stale.status_code == 409
    decided = client.post(
        f"/v1/projects/{project_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "decide-ok", "If-Match": "1"},
        json={"decision": "approve", "expected_intent_hash": digest},
    )
    assert decided.status_code == 202, decided.text
    assert decided.json()["status"] == "accepted"
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        decision = conn.execute(text("SELECT decision FROM approvals WHERE id = :id"), {"id": approval_id}).scalar_one()
    assert decision == "approved"
    assert client.get(f"/v1/projects/{project_id}/approvals").json()["items"] == []


def test_events_and_artifacts_stay_inside_the_project(client, api_db) -> None:
    owner_id, project_id, headers = _session(client)
    other = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "control-artifact-b"}, json={"name": "乙"})
    assert other.status_code == 201, other.text
    accepted = _task(client, headers, project_id, key="task-scope-1")
    leaked_events = client.get(f"/v1/projects/{other.json()['id']}/tasks/{accepted['resource_id']}/events")
    assert leaked_events.status_code == 404
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": "control-upload"},
        files={"file": ("note.txt", "仅甲可见".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    leaked_file = client.get(f"/v1/projects/{other.json()['id']}/artifacts/{uploaded.json()['artifact_id']}")
    assert leaked_file.status_code == 404
    kept = client.get(f"/v1/projects/{project_id}/artifacts/{uploaded.json()['artifact_id']}")
    assert kept.status_code == 200
    assert kept.content == "仅甲可见".encode()


def test_draft_contract_does_not_rewrite_an_approved_contract(client, api_db, role_urls) -> None:
    owner_id, project_id, headers = _session(client)
    accepted = _task(client, headers, project_id, key="task-contract-1")
    task_id = accepted["resource_id"]
    snapshot = client.get(f"/v1/projects/{project_id}/tasks/{task_id}").json()
    digest = "ab" * 32
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, CAST(:content AS jsonb), :digest, :owner, now())"
            ),
            {"project_id": project_id, "task_id": task_id, "content": json.dumps({"goal": "已批准"}), "digest": digest, "owner": owner_id},
        )
    draft = {
        "project_id": project_id,
        "task_id": task_id,
        "contract_version": 2,
        "goal": "新的草案",
        "required_artifacts": ["notes.md"],
        "criteria": [{"criterion_id": "c1", "template": "human_review", "description": "人工看过笔记"}],
        "created_at": "2026-10-15T00:00:00Z",
    }
    revised = client.put(
        f"/v1/projects/{project_id}/tasks/{task_id}/contract",
        headers={**headers, "If-Match": str(snapshot["version"])},
        json=draft,
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["goal"] == "新的草案"
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        stored = conn.execute(text("SELECT content->>'goal' FROM goal_contracts WHERE task_id = :id"), {"id": task_id}).scalar_one()
    assert stored == "已批准"
