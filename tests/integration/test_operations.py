import json

import pytest

from aether_domain.security import sha256_text
from aether_storage.db import Database
from aether_storage.operations import OperationError, OperationLedger
from tests.integration.conftest import insert_task
from tests.integration.test_auth import _bootstrap


def test_approval_is_single_use_and_unknown_is_not_success(client, api_db, role_urls) -> None:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "approval-project"},
        json={"name": "审批"},
    )
    assert project.status_code == 201, project.text
    owner_id = session["owner_id"]
    project_id = project.json()["id"]
    task_id = insert_task(api_db, owner_id, project_id, "审批任务")
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        from sqlalchemy import text

        capability_id = conn.execute(
            text(
                "INSERT INTO capability_grants (project_id, task_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
                "VALUES (:project_id, :task_id, 'worker', :token_hash, 'tool', '{}'::jsonb, now() + interval '10 minutes', 'v1') RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "token_hash": "b" * 64},
        ).scalar_one()
    ledger = OperationLedger(Database(role_urls["aether_worker"]))
    intent = {
        "intent_key": "ext-1",
        "tool_id": "example.write",
        "arguments": {"title": "草稿"},
        "resource_id": "resource-1",
        "resource_version": "v1",
        "risk": "R2",
    }
    digest = sha256_text(json.dumps(intent, sort_keys=True, separators=(",", ":")))
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    with pytest.raises(OperationError):
        ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash="c" * 64)
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=digest)
    calls = []

    def sender(payload):
        calls.append(payload["intent_key"])
        raise TimeoutError

    first = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="non_idempotent", sender=sender)
    assert first["state"] == "UNKNOWN"
    assert calls == ["ext-1"]
    second = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="non_idempotent", sender=sender)
    assert second["replayed"] is True
    assert calls == ["ext-1"]
    reconciled = ledger.reconcile(owner_id=owner_id, project_id=project_id, operation_id=first["operation_id"], reader=lambda _args: {"applied": True, "calls": len(calls)})
    assert reconciled == {"state": "CONFIRMED", "repeated": False}
    assert calls == ["ext-1"]
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        from sqlalchemy import text

        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    assert status == "NEEDS_REVIEW"
    cancel = ledger.request_cancel(owner_id=owner_id, project_id=project_id, task_id=task_id)
    assert cancel["cancel_requested"] is True
    assert cancel["stopped"] is False
    assert cancel["status"] == "NEEDS_REVIEW"
