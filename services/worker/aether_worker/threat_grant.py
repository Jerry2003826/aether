"""One-time threat-browser approval. The verifier confirms; the worker consumes."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database
from aether_worker.loopback import LoopbackError, post_json

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SELECT = (
    "SELECT approval.owner_id::text AS owner_id, approval.intent_digest AS intent_digest, "
    "approval.decision AS decision, approval.consumed_by_operation_id::text AS consumed, "
    "approval.expires_at > now() AS fresh "
    "FROM approvals AS approval "
    "WHERE approval.project_id = :project_id AND approval.task_id = :task_id AND approval.id = :approval_id"
)


class GrantError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def confirm_threat_approval(
    database: Database,
    *,
    project_id: str,
    task_id: str,
    approval_id: str,
    intent_digest: str,
) -> str:
    _ids(project_id, task_id, approval_id, intent_digest)
    with database.transaction(project_id=project_id) as conn:
        row = _row(conn, project_id, task_id, approval_id, lock=False)
    return _check(row, intent_digest)


def consume_threat_approval(
    database: Database,
    *,
    project_id: str,
    task_id: str,
    approval_id: str,
    intent_digest: str,
) -> str:
    _ids(project_id, task_id, approval_id, intent_digest)
    with database.transaction(project_id=project_id) as conn:
        row = _row(conn, project_id, task_id, approval_id, lock=True)
        owner_id = _check(row, intent_digest)
        _consume(conn, project_id, task_id, approval_id, owner_id, intent_digest)
    return owner_id


def worker_authorize(project_id: str, task_id: str, approval_id: str, intent_digest: str) -> str:
    """Ask the verifier, then consume the approval with the worker role."""
    _confirm_remote(project_id, task_id, approval_id, intent_digest)
    url = os.environ.get("AETHER_WORKER_DATABASE_URL", "")
    if not url:
        raise GrantError("approval_required")
    return consume_threat_approval(
        Database(url),
        project_id=project_id,
        task_id=task_id,
        approval_id=approval_id,
        intent_digest=intent_digest,
    )


def _confirm_remote(project_id: str, task_id: str, approval_id: str, intent_digest: str) -> str:
    base = os.environ.get("AETHER_VERIFIER_BASE", "").rstrip("/")
    token = os.environ.get("AETHER_LOOPBACK_TOKEN", "")
    if not base or not token:
        raise GrantError("verifier_required")
    try:
        body = post_json(
            f"{base}/v1/threat-browser/permit",
            token,
            {
                "project_id": project_id,
                "task_id": task_id,
                "approval_id": approval_id,
                "intent_digest": intent_digest,
            },
            timeout=15,
        )
    except LoopbackError as exc:
        raise GrantError("verifier_required") from exc
    code = body.get("error")
    if isinstance(code, str) and code:
        raise GrantError(code)
    owner_id = body.get("owner_id")
    if body.get("digest") != intent_digest or not isinstance(owner_id, str) or owner_id == "":
        raise GrantError("verifier_required")
    return owner_id


def _ids(project_id: str, task_id: str, approval_id: str, intent_digest: str) -> None:
    if _UUID.fullmatch(project_id) is None or _UUID.fullmatch(task_id) is None or _UUID.fullmatch(approval_id) is None:
        raise GrantError("approval_required")
    if _DIGEST.fullmatch(intent_digest) is None:
        raise GrantError("approval_mismatch")


def _row(conn: Any, project_id: str, task_id: str, approval_id: str, *, lock: bool) -> dict[str, Any] | None:
    statement = _SELECT + (" FOR UPDATE OF approval" if lock else "")
    found = conn.execute(
        text(statement),
        {"project_id": project_id, "task_id": task_id, "approval_id": approval_id},
    ).mappings().first()
    if found is None:
        return None
    row = dict(found)
    owner_id = str(row.get("owner_id") or "")
    conn.execute(text("SELECT set_config('aether.owner_id', :owner_id, true)"), {"owner_id": owner_id})
    visible = conn.execute(
        text(
            "SELECT 1 FROM projects WHERE id = :project_id AND deleted_at IS NULL AND owner_id::text = :owner_id"
        ),
        {"project_id": project_id, "owner_id": owner_id},
    ).first()
    row["owner_matches"] = visible is not None
    return row


def _check(row: dict[str, Any] | None, intent_digest: str) -> str:
    if row is None or row.get("owner_matches") is not True:
        raise GrantError("approval_required")
    if row.get("consumed"):
        raise GrantError("approval_consumed")
    if row.get("decision") != "approved" or row.get("fresh") is not True:
        raise GrantError("approval_rejected")
    if row.get("intent_digest") != intent_digest:
        raise GrantError("approval_mismatch")
    return str(row["owner_id"])


def _consume(conn: Any, project_id: str, task_id: str, approval_id: str, owner_id: str, intent_digest: str) -> None:
    arguments = json.dumps({"action": "read_threat_page", "approval_id": approval_id})
    capability_id = conn.execute(
        text(
            "INSERT INTO capability_grants (project_id, task_id, grantee_service, token_hash, capability_type, scope, expires_at, revoked_at, policy_version) "
            "VALUES (:project_id, :task_id, 'aether_worker', :token_hash, 'threat:browse', CAST(:scope AS jsonb), now(), now(), 'v1') "
            "RETURNING id::text"
        ),
        {
            "project_id": project_id,
            "task_id": task_id,
            "token_hash": sha256_text(f"threat.visit:{approval_id}"),
            "scope": arguments,
        },
    ).scalar_one()
    operation_id = conn.execute(
        text(
            "INSERT INTO operations (project_id, task_id, intent_key, tool_id, args_sha256, intent_digest, normalized_arguments, "
            "resource_id, resource_version, idempotency_class, risk, capability_id, approval_id, state, resolved_at) "
            "VALUES (:project_id, :task_id, :intent_key, 'threat.visit', :digest, :digest, CAST(:arguments AS jsonb), "
            ":resource_id, '1', 'non_idempotent', 'R1', :capability_id, :approval_id, 'CONFIRMED', now()) "
            "RETURNING id::text"
        ),
        {
            "project_id": project_id,
            "task_id": task_id,
            "intent_key": f"threat.visit:{approval_id}",
            "digest": intent_digest,
            "arguments": arguments,
            "resource_id": f"approval:{approval_id}",
            "capability_id": capability_id,
            "approval_id": approval_id,
        },
    ).scalar_one()
    consumed = conn.execute(
        text(
            "UPDATE approvals SET consumed_by_operation_id = :operation_id, version = version + 1 "
            "WHERE project_id = :project_id AND id = :approval_id AND consumed_by_operation_id IS NULL"
        ),
        {"operation_id": operation_id, "project_id": project_id, "approval_id": approval_id},
    ).rowcount
    if consumed != 1:
        raise GrantError("approval_consumed")
    if owner_id == "":
        raise GrantError("approval_required")
