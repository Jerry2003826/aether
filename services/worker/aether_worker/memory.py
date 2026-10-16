"""Native PostgreSQL memory. Conflicts stay visible; nothing is silently overwritten."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.db import Database
from aether_worker.retention import invalidate_derived

STATUSES = {"proposed", "source_claim", "user_confirmed", "observed_test", "inference", "superseded"}
KINDS = {"episodic", "semantic", "procedural"}
CATEGORIES = {"fact", "preference", "procedure", "episode", "project_decision"}
ACTIONS = {"confirm", "correct", "supersede"}
CLAIM_CLASSIFICATION = "memory_claim"


class MemoryError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def classify_memory(
    *,
    claim: str,
    source_artifact_ids: list[str],
    requested: str | None = None,
    test_run_verified: bool = False,
) -> str:
    """User input cannot declare itself confirmed. Sources become source claims."""
    if _blocked_claim(claim):
        raise MemoryError("secret_claim_rejected")
    if requested == "user_confirmed":
        raise MemoryError("confirmation_rejected")
    if requested == "inference":
        return "inference"
    if requested == "observed_test":
        if test_run_verified:
            return "observed_test"
        return "proposed"
    if source_artifact_ids:
        return "source_claim"
    return "proposed"


def _is_project_test_run(conn: Any, store: ArtifactStore, *, owner_id: str, project_id: str, artifact_id: object) -> bool:
    if not artifact_id:
        return False
    row = conn.execute(
        text("SELECT media_type FROM artifacts WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
        {"project_id": project_id, "id": str(artifact_id)},
    ).mappings().first()
    if row is None or row["media_type"] != "application/json":
        return False
    try:
        data, media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(artifact_id))
    except ArtifactError:
        return False
    if media != "application/json":
        return False
    try:
        body = json.loads(data.decode())
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(body, dict) and body.get("kind") == "test_run"


def _blocked_claim(claim: str) -> bool:
    folded = claim.casefold()
    return "密钥" in claim or "secret" in folded or "api key" in folded


def _subject(claim: str) -> str:
    for mark in ("：", ":"):
        if mark in claim:
            return claim.split(mark, 1)[0].strip()
    return ""


def estimate_tokens(value: str) -> int:
    return max(1, (len(value) + 1) // 2)


def assemble_context(
    *,
    project_id: str,
    goal: str,
    plan: str,
    evidence: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    skills: list[dict[str, Any]],
    history: str,
    materials: list[dict[str, Any]],
    budget_tokens: int,
    destination: dict[str, Any],
) -> dict[str, Any]:
    """Budgeted prompt. Full history is not copied back in. Export checks run first."""
    dropped: list[dict[str, str]] = []
    parts: list[dict[str, Any]] = []
    remaining = budget_tokens

    def add(name: str, body: str, source_id: str) -> None:
        nonlocal remaining
        tokens = estimate_tokens(body)
        if tokens > remaining and parts:
            dropped.append({"name": name, "reason": "budget", "source_id": source_id})
            return
        used = min(tokens, remaining) if remaining > 0 else 0
        if used <= 0:
            dropped.append({"name": name, "reason": "budget", "source_id": source_id})
            return
        kept = body if used >= tokens else body[: used * 2]
        parts.append({"name": name, "text": kept, "tokens": used, "truncated": used < tokens, "source_id": source_id})
        remaining -= used

    add("goal", goal, "goal")
    add("plan", plan, "plan")
    for item in evidence:
        if not _exportable(item, project_id=project_id, destination=destination, dropped=dropped, name="evidence"):
            continue
        add("evidence", str(item.get("text") or ""), str(item.get("id") or "evidence"))
    for item in memories:
        if item.get("epistemic_status") in {"superseded"} or item.get("deleted_at"):
            dropped.append({"name": "memory", "reason": "inactive", "source_id": str(item.get("id") or "")})
            continue
        if not _exportable(item, project_id=project_id, destination=destination, dropped=dropped, name="memory"):
            continue
        label = f"[{item.get('epistemic_status')}] {item.get('claim') or ''}"
        add("memory", label, str(item.get("id") or "memory"))
    for skill in skills:
        name = str(skill.get("name") or "")
        description = str(skill.get("description") or "")
        add("skill_card", f"{name}: {description}", str(skill.get("id") or name))
        selected = bool(skill.get("selected")) and skill.get("status") == "ACTIVE"
        if not selected:
            if skill.get("instructions"):
                dropped.append({"name": "skill_instructions", "reason": "not_selected", "source_id": str(skill.get("id") or name)})
            continue
        if not _exportable(skill, project_id=project_id, destination=destination, dropped=dropped, name="skill"):
            continue
        add("skill_instructions", str(skill.get("instructions") or ""), str(skill.get("id") or name))
    for item in materials:
        channel = str(item.get("channel") or "material")
        if not _exportable(item, project_id=project_id, destination=destination, dropped=dropped, name=channel):
            continue
        if channel == "codegraph":
            add("untrusted_codegraph", str(item.get("text") or ""), str(item.get("id") or channel))
        else:
            add(channel, str(item.get("text") or ""), str(item.get("id") or channel))
    if history:
        dropped.append({"name": "history", "reason": "not_reinjected", "source_id": "history"})
    text_value = "\n".join(part["text"] for part in parts)
    return {
        "text": text_value,
        "parts": parts,
        "dropped": dropped,
        "budget_tokens": budget_tokens,
        "used_tokens": sum(int(part["tokens"]) for part in parts),
    }


def _exportable(item: dict[str, Any], *, project_id: str, destination: dict[str, Any], dropped: list[dict[str, str]], name: str) -> bool:
    source_id = str(item.get("id") or name)
    if str(item.get("project_id") or project_id) != project_id:
        dropped.append({"name": name, "reason": "cross_project", "source_id": source_id})
        return False
    sensitive = item.get("sensitivity") == "high" or (isinstance(item.get("provenance"), dict) and item["provenance"].get("sensitivity") == "high")
    if sensitive and not destination.get("approved"):
        dropped.append({"name": name, "reason": "export_denied", "source_id": source_id})
        return False
    return True


def remember(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    claim: str,
    kind: str,
    category: str,
    source_artifact_ids: list[str] | None = None,
    requested_status: str | None = None,
    test_run_artifact_id: str | None = None,
    sensitivity: str = "standard",
) -> str:
    """Record one memory directly. Callers cannot pass user_confirmed."""
    memory_id = str(uuid.uuid4())
    payload = {
        "command_id": str(uuid.uuid4()),
        "memory_id": memory_id,
        "claim": claim,
        "kind": kind,
        "category": category,
        "source_artifact_ids": source_artifact_ids or [],
        "requested_status": requested_status,
        "sensitivity": sensitivity,
        "test_run_artifact_id": test_run_artifact_id,
    }
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        _create(conn, store, owner_id=owner_id, project_id=project_id, payload=payload)
    return memory_id


def apply_memory_commands(database: Database, store: ArtifactStore, *, owner_id: str, project_id: str) -> list[str]:
    """Apply accepted memory commands. A repeated command does not insert a second row."""
    applied: list[str] = []
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT id::text AS id, command_key, kind, payload FROM command_outbox "
                "WHERE project_id = :project_id AND kind LIKE 'memory.%' AND status = 'PENDING' "
                "ORDER BY created_at, command_key FOR UPDATE"
            ),
            {"project_id": project_id},
        ).mappings().all()
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
            nested = conn.begin_nested()
            try:
                _apply_one(conn, store, owner_id=owner_id, project_id=project_id, kind=str(row["kind"]), payload=payload)
                nested.commit()
                status = "DELIVERED"
            except OperationalError:
                nested.rollback()
                raise
            except Exception:
                nested.rollback()
                status = "FAILED"
            conn.execute(
                text(
                    "UPDATE command_outbox SET status = :status, delivered_at = CASE WHEN :status = 'DELIVERED' THEN now() ELSE delivered_at END "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"status": status, "project_id": project_id, "id": row["id"]},
            )
            applied.append(str(row["command_key"]))
    return applied


def _already(conn: Any, project_id: str, command_id: str) -> bool:
    found = conn.execute(
        text("SELECT 1 FROM memories WHERE project_id = :project_id AND provenance->>'command_id' = :command_id"),
        {"project_id": project_id, "command_id": command_id},
    ).first()
    return found is not None


def _apply_one(conn: Any, store: ArtifactStore, *, owner_id: str, project_id: str, kind: str, payload: dict[str, Any]) -> None:
    command_id = str(payload["command_id"])
    if _already(conn, project_id, command_id):
        return
    if kind == "memory.create":
        _create(conn, store, owner_id=owner_id, project_id=project_id, payload=payload)
        return
    if kind == "memory.patch":
        _patch(conn, store, owner_id=owner_id, project_id=project_id, payload=payload)
        return
    if kind == "memory.delete":
        _delete(conn, project_id=project_id, payload=payload)
        return
    raise MemoryError("command_rejected")


def _create(conn: Any, store: ArtifactStore, *, owner_id: str, project_id: str, payload: dict[str, Any]) -> None:
    claim = str(payload["claim"])
    sources = [str(item) for item in payload.get("source_artifact_ids") or []]
    for source_id in sources:
        found = conn.execute(
            text("SELECT 1 FROM artifacts WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
            {"project_id": project_id, "id": source_id},
        ).first()
        if found is None:
            raise MemoryError("source_rejected")
    status = classify_memory(
        claim=claim,
        source_artifact_ids=sources,
        requested=payload.get("requested_status"),
        test_run_verified=_is_project_test_run(conn, store, owner_id=owner_id, project_id=project_id, artifact_id=payload.get("test_run_artifact_id")),
    )
    existing = conn.execute(
        text(
            "SELECT id::text AS id, claim, conflict_group::text AS conflict_group FROM memories "
            "WHERE project_id = :project_id AND category = :category AND deleted_at IS NULL AND epistemic_status <> 'superseded'"
        ),
        {"project_id": project_id, "category": payload["category"]},
    ).mappings().all()
    subject = _subject(claim)
    conflict_id = None
    for row in existing:
        if row["claim"] == claim:
            return
        if subject and _subject(str(row["claim"])) == subject:
            conflict_id = row["conflict_group"] or str(uuid.uuid4())
            conn.execute(
                text("UPDATE memories SET conflict_group = :conflict WHERE project_id = :project_id AND id = :id AND conflict_group IS NULL"),
                {"conflict": conflict_id, "project_id": project_id, "id": row["id"]},
            )
    stored = store.put(
        owner_id=owner_id,
        project_id=project_id,
        data=claim.encode(),
        media_type="text/plain",
        classification=CLAIM_CLASSIFICATION,
        conn=conn,
    )
    conn.execute(
        text(
            "INSERT INTO memories (id, project_id, memory_key, revision, kind, claim, category, claim_artifact_id, provenance, epistemic_status, conflict_group) "
            "VALUES (:id, :project_id, :memory_key, 1, :kind, :claim, :category, :artifact_id, CAST(:provenance AS jsonb), :status, :conflict)"
        ),
        {
            "id": payload["memory_id"],
            "project_id": project_id,
            "memory_key": payload["memory_id"],
            "kind": payload["kind"],
            "claim": claim,
            "category": payload["category"],
            "artifact_id": stored["artifact_id"],
            "provenance": json.dumps({"command_id": payload["command_id"], "source_artifact_ids": sources, "sensitivity": payload.get("sensitivity") or "standard"}),
            "status": status,
            "conflict": conflict_id,
        },
    )


def _patch(conn: Any, store: ArtifactStore, *, owner_id: str, project_id: str, payload: dict[str, Any]) -> None:
    action = str(payload["action"])
    if action not in ACTIONS:
        raise MemoryError("action_rejected")
    current = conn.execute(
        text(
            "SELECT id::text AS id, memory_key, revision, kind, claim, category, epistemic_status FROM memories "
            "WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"
        ),
        {"project_id": project_id, "id": payload["memory_id"]},
    ).mappings().first()
    if current is None or int(current["revision"]) != int(payload["expected_revision"]):
        raise MemoryError("version_conflict")
    claim = str(payload["claim"]) if payload.get("claim") else str(current["claim"])
    if _blocked_claim(claim):
        raise MemoryError("secret_claim_rejected")
    status = "user_confirmed" if action == "confirm" else "proposed"
    if action == "confirm" and claim != current["claim"]:
        raise MemoryError("confirmation_rejected")
    stored = store.put(
        owner_id=owner_id,
        project_id=project_id,
        data=claim.encode(),
        media_type="text/plain",
        classification=CLAIM_CLASSIFICATION,
        conn=conn,
    )
    new_id = str(payload["result_id"])
    conn.execute(
        text("UPDATE memories SET epistemic_status = 'superseded' WHERE project_id = :project_id AND id = :id"),
        {"project_id": project_id, "id": current["id"]},
    )
    _retire_claim(conn, project_id=project_id, memory_id=str(current["id"]))
    conn.execute(
        text(
            "INSERT INTO memories (id, project_id, memory_key, revision, kind, claim, category, claim_artifact_id, provenance, epistemic_status, supersedes_id) "
            "VALUES (:id, :project_id, :memory_key, :revision, :kind, :claim, :category, :artifact_id, CAST(:provenance AS jsonb), :status, :supersedes)"
        ),
        {
            "id": new_id,
            "project_id": project_id,
            "memory_key": current["memory_key"],
            "revision": int(current["revision"]) + 1,
            "kind": current["kind"],
            "claim": claim,
            "category": current["category"],
            "artifact_id": stored["artifact_id"],
            "provenance": json.dumps({"command_id": payload["command_id"], "action": action, "sensitivity": "standard"}),
            "status": status,
            "supersedes": current["id"],
        },
    )


def _delete(conn: Any, *, project_id: str, payload: dict[str, Any]) -> None:
    updated = conn.execute(
        text("UPDATE memories SET deleted_at = now() WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL AND revision = :revision"),
        {"project_id": project_id, "id": payload["memory_id"], "revision": int(payload["expected_revision"])},
    ).rowcount
    if updated != 1:
        raise MemoryError("version_conflict")
    _retire_claim(conn, project_id=project_id, memory_id=str(payload["memory_id"]))
    invalidate_derived(conn, project_id=project_id)


def _retire_claim(conn: Any, *, project_id: str, memory_id: str) -> None:
    conn.execute(
        text(
            "UPDATE artifacts SET deleted_at = COALESCE(deleted_at, now()) "
            "WHERE project_id = :project_id AND id = (SELECT claim_artifact_id FROM memories WHERE project_id = :project_id AND id = :id) "
            "AND deleted_at IS NULL"
        ),
        {"project_id": project_id, "id": memory_id},
    )
