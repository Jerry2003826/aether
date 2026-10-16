"""The verifier confirms an approval. The worker consumes it once."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.threat_grant import GrantError, confirm_threat_approval, consume_threat_approval
from tests.integration.conftest import insert_task
from tests.integration.test_console_api import _session

_DIGEST = "ab" * 32
_OTHER = "cd" * 32


def test_verifier_confirms_and_the_worker_consumes_once(client, role_urls) -> None:
    owner_id, project_id, _csrf = _session(client, "threat-grant")
    worker = Database(role_urls["aether_worker"])
    verifier = Database(role_urls["aether_verifier"])
    task_id = insert_task(worker, owner_id, project_id, "威胁情报审批")
    approval_id = _insert_approval(worker, owner_id, project_id, task_id, _DIGEST)
    with pytest.raises(GrantError) as mismatch:
        confirm_threat_approval(
            verifier,
            project_id=project_id,
            task_id=task_id,
            approval_id=approval_id,
            intent_digest=_OTHER,
        )
    assert mismatch.value.code == "approval_mismatch"
    assert _consumed(worker, project_id, approval_id) is None
    confirmed = confirm_threat_approval(
        verifier,
        project_id=project_id,
        task_id=task_id,
        approval_id=approval_id,
        intent_digest=_DIGEST,
    )
    assert confirmed == owner_id
    consumed = consume_threat_approval(
        worker,
        project_id=project_id,
        task_id=task_id,
        approval_id=approval_id,
        intent_digest=_DIGEST,
    )
    assert consumed == owner_id
    operation_id = _consumed(worker, project_id, approval_id)
    assert operation_id is not None
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        tool_id = conn.execute(
            text("SELECT tool_id FROM operations WHERE project_id = :project_id AND id = :operation_id"),
            {"project_id": project_id, "operation_id": operation_id},
        ).scalar_one()
    assert tool_id == "threat.visit"
    with pytest.raises(GrantError) as again:
        consume_threat_approval(
            worker,
            project_id=project_id,
            task_id=task_id,
            approval_id=approval_id,
            intent_digest=_DIGEST,
        )
    assert again.value.code == "approval_consumed"
    with pytest.raises(GrantError) as later:
        confirm_threat_approval(
            verifier,
            project_id=project_id,
            task_id=task_id,
            approval_id=approval_id,
            intent_digest=_DIGEST,
        )
    assert later.value.code == "approval_consumed"


def _insert_approval(database: Database, owner_id: str, project_id: str, task_id: str, digest: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        approval_id = conn.execute(
            text(
                "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, decision, owner_id, expires_at, decided_at) "
                "VALUES (:project_id, :task_id, :digest, '{}'::jsonb, 'approved', :owner, :expires, now()) RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "digest": digest,
                "owner": owner_id,
                "expires": datetime.now(UTC) + timedelta(hours=1),
            },
        ).scalar_one()
    return str(approval_id)


def _consumed(database: Database, project_id: str, approval_id: str) -> str | None:
    with database.transaction(project_id=project_id) as conn:
        value = conn.execute(
            text("SELECT consumed_by_operation_id::text FROM approvals WHERE project_id = :project_id AND id = :approval_id"),
            {"project_id": project_id, "approval_id": approval_id},
        ).scalar_one()
    if value is None:
        return None
    return str(value)
