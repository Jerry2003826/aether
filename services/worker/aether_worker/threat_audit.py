"""Durable threat-browser audit rows. The browser container does not import this module."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database

_ACTION = "threat_browser.visit"
_RESOURCE = "threat_browser"


class AuditError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def persist_threat_audit(database: Database, *, project_id: str, payload: dict[str, object]) -> dict[str, object]:
    """Append one hash-chained row. The caller list is not the record."""
    if project_id == "":
        raise AuditError("audit_failed")
    with database.transaction(project_id=project_id) as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('aether-threat-audit'), hashtext(:project_id))"),
            {"project_id": project_id},
        )
        previous = conn.execute(
            text(
                "SELECT sequence, event_hash FROM audit_events "
                "WHERE project_id = :project_id ORDER BY sequence DESC LIMIT 1"
            ),
            {"project_id": project_id},
        ).mappings().first()
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        previous_hash = None if previous is None else str(previous["event_hash"])
        body = {**payload, "previous_hash": previous_hash, "admissible_evidence": False}
        digest = sha256_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
        owner = str(payload.get("owner_id") or "")
        resource_id = str(payload.get("url_sha256") or payload.get("approval_id") or "threat-browser")
        conn.execute(
            text(
                "INSERT INTO audit_events (project_id, actor_identity, action, resource_type, resource_id, "
                "request_id, payload_redacted, previous_hash, event_hash, sequence) "
                "VALUES (:project_id, :actor, :action, :resource_type, :resource_id, :request_id, "
                "CAST(:payload AS jsonb), :previous_hash, :event_hash, :sequence)"
            ),
            {
                "project_id": project_id,
                "actor": owner if owner else "aether_worker",
                "action": _ACTION,
                "resource_type": _RESOURCE,
                "resource_id": resource_id,
                "request_id": str(uuid.uuid4()),
                "payload": json.dumps(body, sort_keys=True),
                "previous_hash": previous_hash,
                "event_hash": digest,
                "sequence": sequence,
            },
        )
    event: dict[str, Any] = {**body, "event_hash": digest, "sequence": sequence}
    return event
