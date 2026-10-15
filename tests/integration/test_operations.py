import json
import uuid

import pytest
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database
from aether_storage.operations import OperationError, OperationLedger
from tests.integration.conftest import insert_task
from tests.integration.test_auth import _bootstrap


def _ledger(client, api_db, role_urls, key: str):
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": f"approval-{key}"},
        json={"name": key},
    )
    assert project.status_code == 201, project.text
    owner_id = session["owner_id"]
    project_id = project.json()["id"]
    task_id = insert_task(api_db, owner_id, project_id, key)
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        capability_id = conn.execute(
            text(
                "INSERT INTO capability_grants (project_id, task_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
                "VALUES (:project_id, :task_id, 'worker', :token_hash, 'tool', '{}'::jsonb, now() + interval '10 minutes', 'v1') RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "token_hash": uuid.uuid4().hex + uuid.uuid4().hex},
        ).scalar_one()
    return owner_id, project_id, task_id, str(capability_id), OperationLedger(Database(role_urls["aether_worker"]))


def _intent(key: str, version: str = "v1") -> dict:
    return {
        "intent_key": key,
        "tool_id": "example.write",
        "arguments": {"title": "草稿"},
        "resource_id": "resource-1",
        "resource_version": version,
        "risk": "R2",
    }


def _digest(intent: dict) -> str:
    return sha256_text(json.dumps(intent, sort_keys=True, separators=(",", ":")))


def test_approval_rejects_wrong_hash_owner_and_expiry(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, _capability, ledger = _ledger(client, api_db, role_urls, "bind")
    intent = _intent("bind-1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2", ttl_seconds=-1)
    with pytest.raises(OperationError) as expired:
        ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    assert expired.value.code == "approval_rejected"
    fresh = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    with pytest.raises(OperationError) as wrong_hash:
        ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=fresh, decision="approved", expected_intent_hash="c" * 64)
    assert wrong_hash.value.code == "approval_rejected"
    with pytest.raises(OperationError) as wrong_owner:
        ledger.decide(owner_id=str(uuid.uuid4()), project_id=project_id, task_id=task_id, approval_id=fresh, decision="approved", expected_intent_hash=_digest(intent))
    assert wrong_owner.value.code == "approval_rejected"


def test_rejected_approval_cannot_dispatch(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "reject")
    intent = _intent("reject-1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="rejected", expected_intent_hash=_digest(intent))
    calls = []
    with pytest.raises(OperationError) as rejected:
        ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="idempotent_write", sender=lambda payload: calls.append(payload))
    assert rejected.value.code == "approval_rejected"
    assert calls == []


def test_changed_resource_version_requires_new_approval(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "version")
    intent = _intent("version-1", "v1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    changed = _intent("version-1", "v2")
    calls = []
    with pytest.raises(OperationError):
        ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=changed, capability_id=capability_id, idempotency="idempotent_write", sender=lambda payload: calls.append(payload))
    assert calls == []


def test_lost_success_response_does_not_repeat_side_effect(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "lost")
    intent = _intent("lost-1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id=intent["resource_id"], version=intent["resource_version"])
    remote = {}

    def sender(payload):
        remote["applied"] = payload["intent_key"]
        raise TimeoutError

    first = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="reconcilable_write", sender=sender)
    replay = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="reconcilable_write", sender=sender)
    assert first["state"] == "UNKNOWN"
    assert replay["replayed"] is True
    reconciled = ledger.reconcile(owner_id=owner_id, project_id=project_id, operation_id=first["operation_id"], reader=lambda _args: {"applied": remote["applied"], "resource_version": "v1"})
    assert reconciled["state"] == "CONFIRMED"
    assert reconciled["mismatch"] is False
    assert remote["applied"] == "lost-1"


def test_reconcile_mismatch_does_not_confirm(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "mismatch")
    intent = _intent("mismatch-1", "v1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id=intent["resource_id"], version=intent["resource_version"])
    dispatched = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="reconcilable_write", sender=lambda _payload: (_ for _ in ()).throw(TimeoutError))
    result = ledger.reconcile(owner_id=owner_id, project_id=project_id, operation_id=dispatched["operation_id"], reader=lambda _args: {"resource_version": "v9"})
    assert result["mismatch"] is True
    assert result["state"] == "UNKNOWN"


def test_non_idempotent_unknown_needs_review_and_cancel_is_not_stopped(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "review")
    intent = _intent("review-1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id=intent["resource_id"], version=intent["resource_version"])
    outcome = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="non_idempotent", sender=lambda _payload: (_ for _ in ()).throw(TimeoutError))
    assert outcome["state"] == "UNKNOWN"
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        role = conn.execute(text("SELECT current_user")).scalar_one()
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    assert role == "aether_worker"
    assert status == "NEEDS_REVIEW"
    cancel = ledger.request_cancel(owner_id=owner_id, project_id=project_id, task_id=task_id)
    assert cancel["cancel_requested"] is True
    assert cancel["stopped"] is False
    assert cancel["status"] != "CANCELLED"


def test_connection_reset_is_unknown_and_needs_review(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "reset")
    intent = _intent("reset-1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id=intent["resource_id"], version="v1")

    def sender(_payload):
        raise ConnectionResetError("simulated reset")

    outcome = ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="non_idempotent", sender=sender)
    assert outcome["state"] == "UNKNOWN"
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    assert status == "NEEDS_REVIEW"


def test_dispatch_rejects_stale_live_resource_version(client, api_db, role_urls) -> None:
    owner_id, project_id, task_id, capability_id, ledger = _ledger(client, api_db, role_urls, "stale")
    intent = _intent("stale-1", "v1")
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id="resource-1", version="v1")
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, decision="approved", expected_intent_hash=_digest(intent))
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id="resource-1", version="v2")
    calls = []
    with pytest.raises(OperationError) as stale:
        ledger.dispatch(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=approval_id, intent=intent, capability_id=capability_id, idempotency="idempotent_write", sender=lambda payload: calls.append(payload))
    assert stale.value.code == "stale_resource"
    assert calls == []
