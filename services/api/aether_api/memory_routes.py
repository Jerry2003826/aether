"""Memory commands. Accepting a command does not confirm the claim."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aether_api.control import _accepted, _claim_idempotency, _decode_cursor, _encode_cursor, _idempotency_key, _owned, _owner, _stamp, _store_idempotency, _db, _task_id
from aether_api.errors import ApiError
from aether_api.settings import Settings
from aether_domain.security import sha256_text
from aether_worker.memory import ACTIONS, CATEGORIES, KINDS, MemoryError, classify_memory

def register_memory_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/v1/projects/{project_id}/memories", operation_id="listMemories")
    def list_memories(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        if limit < 1 or limit > 100:
            raise ApiError(422, "invalid_limit", "limit 必须在 1 到 100 之间")
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, claim, kind, revision, epistemic_status, provenance, valid_from, valid_to, "
                    "conflict_group::text AS conflict_group, category, created_at FROM memories "
                    "WHERE project_id = :project_id AND deleted_at IS NULL "
                    "AND (CAST(:created_at AS timestamptz) IS NULL OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
        items = [_public(row) for row in rows]
        next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if len(items) == limit else None
        return {"items": items, "next_cursor": next_cursor}

    @app.post("/v1/projects/{project_id}/memories", status_code=202, operation_id="createMemory")
    def create_memory(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        key = _idempotency_key(request)
        payload = _create_body(body)
        try:
            classify_memory(claim=payload["claim"], source_artifact_ids=payload["source_artifact_ids"])
        except MemoryError as exc:
            raise ApiError(422, exc.code, "这条记忆不能写入") from exc
        memory_id = str(uuid.uuid4())
        command_id = str(uuid.uuid4())
        command = {"command_id": command_id, "memory_id": memory_id, "owner_id": owner, **payload}
        route = f"POST /v1/projects/{project_id}/memories"
        digest = sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            existing = _claim_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, payload_hash=digest)
            if existing is not None:
                return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
            _enqueue(conn, project_id=project_id, command_key=command_id, kind="memory.create", payload=command)
            accepted = _accepted(request, command_id, memory_id)
            _store_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, body=accepted)
        return JSONResponse(status_code=202, content=accepted)

    @app.patch("/v1/projects/{project_id}/memories/{memory_id}", status_code=202, operation_id="updateMemory")
    def patch_memory(project_id: str, memory_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        _task_id(memory_id)
        key = _idempotency_key(request)
        match = request.headers.get("if-match")
        if match is None or not match.isdigit():
            raise ApiError(409, "version_conflict", "缺少记忆版本")
        parsed = _patch_body(body)
        command_id = str(uuid.uuid4())
        result_id = str(uuid.uuid4())
        command = {
            "command_id": command_id,
            "memory_id": memory_id,
            "result_id": result_id,
            "action": parsed["action"],
            "claim": parsed["claim"],
            "expected_revision": int(match),
            "owner_id": owner,
        }
        if parsed["action"] == "declassify":
            command["sensitivity"] = parsed["sensitivity"]
            command["authorization_id"] = parsed["authorization_id"]
        route = f"PATCH /v1/projects/{project_id}/memories/{memory_id}"
        digest_body = {"action": parsed["action"], "claim": parsed["claim"], "revision": int(match)}
        if parsed["action"] == "declassify":
            digest_body["sensitivity"] = parsed["sensitivity"]
            digest_body["authorization_id"] = parsed["authorization_id"]
        digest = sha256_text(json.dumps(digest_body, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            existing = _claim_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, payload_hash=digest)
            if existing is not None:
                return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
            current = conn.execute(
                text("SELECT revision FROM memories WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
                {"project_id": project_id, "id": memory_id},
            ).scalar()
            if current is None or int(current) != int(match):
                raise ApiError(409, "version_conflict", "记忆版本已变化")
            _enqueue(conn, project_id=project_id, command_key=command_id, kind="memory.patch", payload=command)
            accepted = _accepted(request, command_id, result_id)
            _store_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, body=accepted)
        return JSONResponse(status_code=202, content=accepted)

    @app.delete("/v1/projects/{project_id}/memories/{memory_id}", status_code=202, operation_id="deleteMemory")
    def delete_memory(project_id: str, memory_id: str, request: Request) -> JSONResponse:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        _task_id(memory_id)
        key = _idempotency_key(request)
        match = request.headers.get("if-match")
        if match is None or not match.isdigit():
            raise ApiError(409, "version_conflict", "缺少记忆版本")
        command_id = str(uuid.uuid4())
        command = {"command_id": command_id, "memory_id": memory_id, "expected_revision": int(match)}
        route = f"DELETE /v1/projects/{project_id}/memories/{memory_id}"
        digest = sha256_text(json.dumps(command, sort_keys=True, separators=(",", ":")))
        command = {**command, "owner_id": owner}
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            existing = _claim_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, payload_hash=digest)
            if existing is not None:
                return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
            current = conn.execute(
                text("SELECT revision FROM memories WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
                {"project_id": project_id, "id": memory_id},
            ).scalar()
            if current is None or int(current) != int(match):
                raise ApiError(409, "version_conflict", "记忆版本已变化")
            _enqueue(conn, project_id=project_id, command_key=command_id, kind="memory.delete", payload=command)
            accepted = _accepted(request, command_id, memory_id)
            _store_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, body=accepted)
        return JSONResponse(status_code=202, content=accepted)


def _create_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) - {"claim", "kind", "category", "source_artifact_ids"}:
        raise ApiError(422, "invalid_memory", "记忆请求不合法")
    claim = body.get("claim")
    kind = body.get("kind")
    category = body.get("category")
    sources = body.get("source_artifact_ids") or []
    if not isinstance(claim, str) or not claim.strip() or kind not in KINDS or category not in CATEGORIES or not isinstance(sources, list):
        raise ApiError(422, "invalid_memory", "记忆请求不合法")
    for item in sources:
        try:
            uuid.UUID(str(item))
        except ValueError as exc:
            raise ApiError(422, "invalid_memory", "来源编号不合法") from exc
    return {"claim": claim.strip(), "kind": kind, "category": category, "source_artifact_ids": [str(item) for item in sources]}


def _patch_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("action") not in ACTIONS:
        raise ApiError(422, "invalid_memory", "记忆修正不合法")
    action = str(body["action"])
    if action == "declassify":
        return _declassify_body(body)
    if set(body) - {"claim", "action"}:
        raise ApiError(422, "invalid_memory", "记忆修正不合法")
    claim = body.get("claim")
    if claim is not None and (not isinstance(claim, str) or not claim.strip()):
        raise ApiError(422, "invalid_memory", "记忆修正不合法")
    if action in {"correct", "supersede"} and not isinstance(claim, str):
        raise ApiError(422, "invalid_memory", "修正必须带新的说法")
    return {"action": action, "claim": None if claim is None else claim.strip(), "sensitivity": None, "authorization_id": None}


def _declassify_body(body: dict[str, Any]) -> dict[str, Any]:
    if set(body) - {"action", "sensitivity", "authorization_id"}:
        raise ApiError(422, "invalid_memory", "记忆修正不合法")
    sensitivity = body.get("sensitivity")
    authorization_id = body.get("authorization_id")
    if sensitivity not in {"standard", "high"}:
        raise ApiError(422, "invalid_memory", "记忆修正不合法")
    try:
        uuid.UUID(str(authorization_id))
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid_memory", "记忆修正不合法") from exc
    return {"action": "declassify", "claim": None, "sensitivity": str(sensitivity), "authorization_id": str(authorization_id)}


def _enqueue(conn: Any, *, project_id: str, command_key: str, kind: str, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    conn.execute(
        text(
            "INSERT INTO command_outbox (project_id, command_key, kind, payload, payload_sha256) "
            "VALUES (:project_id, :command_key, :kind, CAST(:payload AS jsonb), :digest)"
        ),
        {"project_id": project_id, "command_key": command_key, "kind": kind, "payload": raw, "digest": sha256_text(raw)},
    )


def _public(row: Any) -> dict[str, Any]:
    provenance = row["provenance"] if isinstance(row["provenance"], dict) else json.loads(row["provenance"])
    return {
        "id": row["id"],
        "claim": row["claim"],
        "kind": row["kind"],
        "revision": int(row["revision"]),
        "epistemic_status": row["epistemic_status"],
        "provenance": provenance,
        "valid_from": _stamp(row["valid_from"]),
        "valid_to": _stamp(row["valid_to"]),
        "conflict_group": row["conflict_group"],
        "category": row["category"],
    }
