"""Approval consumption and operation reconciliation. Cancellation is not completion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database

_TRANSPORT = (TimeoutError, ConnectionError, BrokenPipeError, ConnectionAbortedError, ConnectionResetError, httpx.TransportError)


def _outcome_unknown(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TRANSPORT):
            return True
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return False


class OperationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OperationLedger:
    def __init__(self, database: Database) -> None:
        self.database = database

    def request_approval(self, *, owner_id: str, project_id: str, task_id: str, intent: dict, risk: str, ttl_seconds: int = 300) -> str:
        digest = sha256_text(json.dumps(intent, sort_keys=True, separators=(",", ":")))
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            approval_id = conn.execute(
                text(
                    "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, owner_id, expires_at) "
                    "VALUES (:project_id, :task_id, :digest, CAST(:payload AS jsonb), :owner_id, :expires_at) "
                    "RETURNING id::text"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "digest": digest,
                    "payload": json.dumps({**intent, "risk": risk}),
                    "owner_id": owner_id,
                    "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                },
            ).scalar_one()
        return str(approval_id)

    def decide(self, *, owner_id: str, project_id: str, task_id: str, approval_id: str, decision: str, expected_intent_hash: str) -> None:
        if decision not in {"approved", "rejected"}:
            raise OperationError("invalid_decision")
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            updated = conn.execute(
                text(
                    "UPDATE approvals SET decision = :decision, decided_at = now(), version = version + 1 "
                    "WHERE id = :id AND task_id = :task_id AND owner_id = :owner_id AND decision = 'pending' "
                    "AND intent_digest = :digest AND expires_at > now()"
                ),
                {"decision": decision, "id": approval_id, "task_id": task_id, "owner_id": owner_id, "digest": expected_intent_hash},
            ).rowcount
        if updated != 1:
            raise OperationError("approval_rejected")

    def dispatch(self, *, owner_id: str, project_id: str, task_id: str, approval_id: str, intent: dict, capability_id: str, idempotency: str, sender) -> dict:
        digest = sha256_text(json.dumps(intent, sort_keys=True, separators=(",", ":")))
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            approval = conn.execute(
                text(
                    "SELECT decision, intent_digest, expires_at, consumed_by_operation_id, version "
                    "FROM approvals WHERE id = :id AND task_id = :task_id AND owner_id = :owner_id FOR UPDATE"
                ),
                {"id": approval_id, "task_id": task_id, "owner_id": owner_id},
            ).mappings().first()
            if approval is None or approval["decision"] != "approved" or approval["intent_digest"] != digest or approval["expires_at"] <= datetime.now(UTC):
                raise OperationError("approval_rejected")
            if approval["consumed_by_operation_id"] is None:
                # Pre-check only. This lock ends when the transaction commits, before sender() runs.
                current_version = conn.execute(
                    text(
                        "SELECT version FROM resource_heads WHERE project_id = :project_id AND resource_id = :resource_id FOR UPDATE"
                    ),
                    {"project_id": project_id, "resource_id": intent["resource_id"]},
                ).scalar()
                if current_version != intent["resource_version"]:
                    raise OperationError("stale_resource")
            if approval["consumed_by_operation_id"] is not None:
                existing = conn.execute(
                    text("SELECT id::text, state, result FROM operations WHERE id = :id"),
                    {"id": approval["consumed_by_operation_id"]},
                ).mappings().one()
                return {"operation_id": existing["id"], "state": existing["state"], "replayed": True, "result": existing["result"]}
            operation_id = conn.execute(
                text(
                    "INSERT INTO operations (project_id, task_id, intent_key, tool_id, args_sha256, intent_digest, normalized_arguments, "
                    "resource_id, resource_version, idempotency_class, risk, capability_id, approval_id, state) "
                    "VALUES (:project_id, :task_id, :intent_key, :tool_id, :args_sha256, :digest, CAST(:args AS jsonb), "
                    ":resource_id, :resource_version, :idempotency, :risk, :capability_id, :approval_id, 'DISPATCHED') "
                    "RETURNING id::text"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "intent_key": intent["intent_key"],
                    "tool_id": intent["tool_id"],
                    "args_sha256": digest,
                    "digest": digest,
                    "args": json.dumps(intent["arguments"]),
                    "resource_id": intent["resource_id"],
                    "resource_version": intent["resource_version"],
                    "idempotency": idempotency,
                    "risk": intent["risk"],
                    "capability_id": capability_id,
                    "approval_id": approval_id,
                },
            ).scalar_one()
            consumed = conn.execute(
                text(
                    "UPDATE approvals SET consumed_by_operation_id = :operation_id, version = version + 1 "
                    "WHERE id = :id AND consumed_by_operation_id IS NULL"
                ),
                {"operation_id": operation_id, "id": approval_id},
            ).rowcount
            if consumed != 1:
                raise OperationError("approval_consumed")
        try:
            remote = sender(intent)
        except Exception as exc:
            if not _outcome_unknown(exc):
                raise
            self._set_state(owner_id, project_id, operation_id, "UNKNOWN", None)
            if idempotency == "non_idempotent":
                self._mark_task(owner_id, project_id, task_id, "NEEDS_REVIEW")
            return {"operation_id": str(operation_id), "state": "UNKNOWN", "replayed": False, "result": None}
        self._set_state(owner_id, project_id, operation_id, "CONFIRMED", remote)
        self._record_reported_version(owner_id=owner_id, project_id=project_id, resource_id=intent["resource_id"], remote=remote)
        return {"operation_id": str(operation_id), "state": "CONFIRMED", "replayed": False, "result": remote}

    def reconcile(self, *, owner_id: str, project_id: str, operation_id: str, reader) -> dict:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            operation = conn.execute(
                text("SELECT state, normalized_arguments, idempotency_class, resource_id, resource_version FROM operations WHERE id = :id FOR UPDATE"),
                {"id": operation_id},
            ).mappings().one()
        if operation["state"] == "CONFIRMED":
            return {"state": "CONFIRMED", "repeated": False, "mismatch": False}
        remote = reader(operation["normalized_arguments"])
        if remote is None:
            return {"state": operation["state"], "repeated": False, "mismatch": False}
        remote_version = remote.get("resource_version")
        if remote_version is not None and remote_version != operation["resource_version"]:
            return {"state": operation["state"], "repeated": False, "mismatch": True}
        self._set_state(owner_id, project_id, operation_id, "CONFIRMED", remote)
        self._record_reported_version(owner_id=owner_id, project_id=project_id, resource_id=operation["resource_id"], remote=remote)
        return {"state": "CONFIRMED", "repeated": False, "mismatch": False}

    def note_resource_version(self, *, owner_id: str, project_id: str, resource_id: str, version: str) -> None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            current = conn.execute(
                text("SELECT version FROM resource_heads WHERE project_id = :project_id AND resource_id = :resource_id FOR UPDATE"),
                {"project_id": project_id, "resource_id": resource_id},
            ).scalar()
            if current is None:
                conn.execute(
                    text(
                        "INSERT INTO resource_heads (project_id, resource_id, version) VALUES (:project_id, :resource_id, :version) "
                        "ON CONFLICT (project_id, resource_id) DO UPDATE SET version = EXCLUDED.version"
                    ),
                    {"project_id": project_id, "resource_id": resource_id, "version": version},
                )
                return
            conn.execute(
                text("UPDATE resource_heads SET version = :version WHERE project_id = :project_id AND resource_id = :resource_id"),
                {"project_id": project_id, "resource_id": resource_id, "version": version},
            )

    def _record_reported_version(self, *, owner_id: str, project_id: str, resource_id: str, remote: object) -> None:
        if not isinstance(remote, dict):
            return
        version = remote.get("resource_version")
        if not isinstance(version, str) or not version:
            return
        self.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id=resource_id, version=version)

    def request_cancel(self, *, owner_id: str, project_id: str, task_id: str) -> dict:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text("UPDATE tasks SET resume_state = 'cancel_requested', version = version + 1 WHERE id = :id AND status <> 'CANCELLED'"),
                {"id": task_id},
            )
            status = conn.execute(text("SELECT status, resume_state FROM tasks WHERE id = :id"), {"id": task_id}).mappings().one()
        return {"status": status["status"], "cancel_requested": status["resume_state"] == "cancel_requested", "stopped": status["status"] == "CANCELLED"}

    def _set_state(self, owner_id: str, project_id: str, operation_id: str, state: str, result) -> None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text("UPDATE operations SET state = :state, result = CAST(:result AS jsonb), resolved_at = CASE WHEN :state = 'CONFIRMED' THEN now() ELSE resolved_at END WHERE id = :id"),
                {"state": state, "result": json.dumps(result) if result is not None else None, "id": operation_id},
            )

    def _mark_task(self, owner_id: str, project_id: str, task_id: str, status: str) -> None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "UPDATE tasks SET status = :status, version = version + 1 "
                    "WHERE id = :id AND project_id = :project_id AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')"
                ),
                {"status": status, "id": task_id, "project_id": project_id},
            )
