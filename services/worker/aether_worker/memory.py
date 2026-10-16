"""Native PostgreSQL memory. Conflicts stay visible; nothing is silently overwritten."""

from __future__ import annotations

import hashlib
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
ACTIONS = {"confirm", "correct", "supersede", "declassify"}
CLAIM_CLASSIFICATION = "memory_claim"
TEST_RUN_CRITERION = "test_run"
TRUSTED_TEST_ISSUER = "aether_verifier"
DECLASSIFY_CAPABILITY = "memory_declassify"
_SENSITIVITY_RANK = {"standard": 0, "high": 1}
_FAILED_RUN_STATUS = {"failed", "fail", "error"}


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
    """A user-supplied kind label is not a test run.

    The artifact must already be committed, and a verifier ledger row must bind
    its PASS conclusion to the hash of the bytes we read now.
    """
    if not artifact_id:
        return False
    identity = str(artifact_id)
    row = conn.execute(
        text("SELECT media_type, sha256 FROM artifacts WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
        {"project_id": project_id, "id": identity},
    ).mappings().first()
    if row is None or row["media_type"] != "application/json":
        return False
    try:
        data, media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=identity)
    except ArtifactError:
        return False
    if media != "application/json":
        return False
    digest = hashlib.sha256(data).hexdigest()
    if digest != str(row["sha256"]):
        return False
    if _result_records_failure(data):
        return False
    verdicts = _trusted_test_verdicts(conn, project_id=project_id, artifact_id=identity, digest=digest)
    return bool(verdicts) and all(item == "PASS" for item in verdicts)


def _result_records_failure(data: bytes) -> bool:
    try:
        body = json.loads(data.decode())
    except (UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(body, dict):
        return False
    status = body.get("status")
    if isinstance(status, str) and status.casefold() in _FAILED_RUN_STATUS:
        return True
    exit_code = body.get("exit_code")
    if type(exit_code) is int and exit_code != 0:
        return True
    return body.get("passed") is False


def _trusted_test_verdicts(conn: Any, *, project_id: str, artifact_id: str, digest: str) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT verdict, artifact_sha256, observed_sha256, verifier_identity FROM verification_results "
            "WHERE project_id = :project_id AND artifact_id = :artifact_id AND criterion_id = :criterion_id"
        ),
        {"project_id": project_id, "artifact_id": artifact_id, "criterion_id": TEST_RUN_CRITERION},
    ).mappings().all()
    verdicts: list[str] = []
    for row in rows:
        if str(row["verifier_identity"]) != TRUSTED_TEST_ISSUER:
            continue
        if str(row["artifact_sha256"]) != digest:
            continue
        observed = row["observed_sha256"]
        if observed is not None and str(observed) != digest:
            continue
        verdicts.append(str(row["verdict"]))
    return verdicts


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


def authorize_declassification(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    memory_id: str,
    from_sensitivity: str,
    to_sensitivity: str,
) -> str:
    """Record a one-time grant. Confirmation and correction never call this."""
    if not _lowers_sensitivity(from_sensitivity, to_sensitivity):
        raise MemoryError("declassification_rejected")
    grant_id = str(uuid.uuid4())
    token_hash = hashlib.sha256(f"{grant_id}:{memory_id}:{to_sensitivity}".encode()).hexdigest()
    scope = json.dumps({"memory_id": memory_id, "from_sensitivity": from_sensitivity, "to_sensitivity": to_sensitivity})
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        current = conn.execute(
            text("SELECT provenance FROM memories WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
            {"project_id": project_id, "id": memory_id},
        ).scalar()
        if current is None or _sensitivity(_provenance_map(current).get("sensitivity")) != from_sensitivity:
            raise MemoryError("declassification_rejected")
        conn.execute(
            text(
                "INSERT INTO capability_grants (id, project_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
                "VALUES (:id, :project_id, 'aether_worker', :token_hash, :capability, CAST(:scope AS jsonb), now() + interval '1 day', 'memory-declassify-1')"
            ),
            {
                "id": grant_id,
                "project_id": project_id,
                "token_hash": token_hash,
                "capability": DECLASSIFY_CAPABILITY,
                "scope": scope,
            },
        )
    return grant_id


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


def _provenance_map(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _source_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _sensitivity(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "standard"


def _lowers_sensitivity(current: str, requested: str) -> bool:
    if requested == current:
        return False
    current_rank = _SENSITIVITY_RANK.get(current)
    requested_rank = _SENSITIVITY_RANK.get(requested)
    if current_rank is None or requested_rank is None:
        return True
    return requested_rank < current_rank


def _revision_provenance(current: object, *, command_id: str, action: str, sensitivity: str) -> dict[str, Any]:
    previous = _provenance_map(current)
    return {
        "command_id": command_id,
        "action": action,
        "sensitivity": sensitivity,
        "source_artifact_ids": _source_ids(previous.get("source_artifact_ids")),
    }


def _consume_declassification(
    conn: Any,
    *,
    project_id: str,
    memory_id: str,
    current: str,
    requested: str,
    authorization_id: object,
) -> None:
    if not isinstance(authorization_id, str) or not authorization_id:
        raise MemoryError("declassification_unauthorized")
    row = conn.execute(
        text(
            "SELECT scope FROM capability_grants "
            "WHERE project_id = :project_id AND id = :id AND capability_type = :capability "
            "AND revoked_at IS NULL AND expires_at > now() FOR UPDATE"
        ),
        {"project_id": project_id, "id": authorization_id, "capability": DECLASSIFY_CAPABILITY},
    ).mappings().first()
    if row is None:
        raise MemoryError("declassification_unauthorized")
    scope = row["scope"] if isinstance(row["scope"], dict) else _provenance_map(row["scope"])
    if scope.get("memory_id") != memory_id or scope.get("from_sensitivity") != current or scope.get("to_sensitivity") != requested:
        raise MemoryError("declassification_unauthorized")
    updated = conn.execute(
        text(
            "UPDATE capability_grants SET revoked_at = now() "
            "WHERE project_id = :project_id AND id = :id AND revoked_at IS NULL"
        ),
        {"project_id": project_id, "id": authorization_id},
    ).rowcount
    if updated != 1:
        raise MemoryError("declassification_unauthorized")


def _patch(conn: Any, store: ArtifactStore, *, owner_id: str, project_id: str, payload: dict[str, Any]) -> None:
    action = str(payload["action"])
    if action not in ACTIONS:
        raise MemoryError("action_rejected")
    current = conn.execute(
        text(
            "SELECT id::text AS id, memory_key, revision, kind, claim, category, epistemic_status, provenance FROM memories "
            "WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"
        ),
        {"project_id": project_id, "id": payload["memory_id"]},
    ).mappings().first()
    if current is None or int(current["revision"]) != int(payload["expected_revision"]):
        raise MemoryError("version_conflict")
    claim = str(payload["claim"]) if payload.get("claim") else str(current["claim"])
    if _blocked_claim(claim):
        raise MemoryError("secret_claim_rejected")
    if action in {"confirm", "declassify"} and claim != current["claim"]:
        raise MemoryError("confirmation_rejected")
    inherited = _sensitivity(_provenance_map(current["provenance"]).get("sensitivity"))
    if action == "declassify":
        requested = payload.get("sensitivity")
        if not isinstance(requested, str) or not _lowers_sensitivity(inherited, requested.strip()):
            raise MemoryError("declassification_rejected")
        sensitivity = requested.strip()
        _consume_declassification(
            conn,
            project_id=project_id,
            memory_id=str(current["id"]),
            current=inherited,
            requested=sensitivity,
            authorization_id=payload.get("authorization_id"),
        )
        status = str(current["epistemic_status"])
    else:
        sensitivity = inherited
        status = "user_confirmed" if action == "confirm" else "proposed"
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
            "provenance": json.dumps(
                _revision_provenance(current["provenance"], command_id=str(payload["command_id"]), action=action, sensitivity=sensitivity)
            ),
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
