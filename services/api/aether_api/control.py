"""Task, event, and approval routes. Accepted commands are not task success."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aether_api.errors import ApiError
from aether_api.settings import Settings
from aether_domain.security import sha256_text
from aether_storage.db import Database
from aether_storage.operations import OperationError, OperationLedger
from aether_worker.outbox import insert_followup_command, insert_start_command

_COMMANDS = {"approve_goal", "pause", "resume", "cancel"}
_TEMPLATES = {
    "artifact_exists",
    "json_schema",
    "trusted_test_suite",
    "citation_integrity",
    "source_coverage",
    "human_review",
}


def register_control_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/v1/projects/{project_id}/tasks", operation_id="listTasks")
    def list_tasks(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        if limit < 1 or limit > 100:
            raise ApiError(422, "invalid_limit", "limit 必须在 1 到 100 之间")
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id FROM tasks WHERE project_id = :project_id "
                    "AND (CAST(:created_at AS timestamptz) IS NULL "
                    "OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
            items = [_snapshot(conn, project_id, row["id"]) for row in rows]
        next_cursor = None
        if len(items) == limit:
            next_cursor = _encode_cursor(items[-1]["created_at"], items[-1]["id"])
        return {"items": [_public_task(item) for item in items], "next_cursor": next_cursor}

    @app.post("/v1/projects/{project_id}/tasks", status_code=202, operation_id="createTask")
    def create_task(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        key = _idempotency_key(request)
        goal = body.get("goal") if isinstance(body, dict) else None
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 16000 or set(body) - {"goal", "budget", "allowed_source_ids"}:
            raise ApiError(422, "invalid_task", "任务目标不合法")
        budget = _budget_spec(body.get("budget")) if "budget" in body else None
        allowed_source_ids = _source_ids(body.get("allowed_source_ids")) if "allowed_source_ids" in body else None
        payload_hash = sha256_text(json.dumps({"goal": goal, "budget": budget, "allowed_source_ids": allowed_source_ids}, sort_keys=True, separators=(",", ":")))
        route = f"POST /v1/projects/{project_id}/tasks"
        command_id = str(uuid.uuid4())
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            existing = _claim_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, payload_hash=payload_hash)
            if existing is not None:
                return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
            created = insert_start_command(
                conn,
                project_id=project_id,
                goal=goal.strip(),
                command_key=command_id,
                owner_id=owner,
                budget=budget,
                allowed_source_ids=allowed_source_ids,
            )
            _append_event(conn, project_id=project_id, task_id=created["task_id"], event_type="task.accepted", payload={"goal": goal.strip(), "status": "QUEUED"})
            accepted = _accepted(request, command_id, created["task_id"])
            _store_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, body=accepted)
        return JSONResponse(status_code=202, content=accepted)

    @app.get("/v1/projects/{project_id}/tasks/{task_id}", operation_id="getTask")
    def get_task(project_id: str, task_id: str, request: Request) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _task_id(task_id)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            snapshot = _snapshot(conn, project_id, task_id)
        return _public_task(snapshot)

    @app.get("/v1/projects/{project_id}/tasks/{task_id}/contract", operation_id="getContract")
    def get_contract(project_id: str, task_id: str, request: Request) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _task_id(task_id)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            draft = conn.execute(
                text("SELECT draft_contract FROM tasks WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": task_id},
            ).scalar()
        if not isinstance(draft, dict):
            raise ApiError(404, "not_found", "还没有合同草案")
        return draft

    @app.put("/v1/projects/{project_id}/tasks/{task_id}/contract", operation_id="reviseContract")
    def revise_contract(project_id: str, task_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        _task_id(task_id)
        match = request.headers.get("if-match")
        draft = _draft_contract(body, project_id, task_id)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            status = conn.execute(
                text("SELECT status FROM tasks WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": task_id},
            ).scalar()
            if status is None:
                raise ApiError(404, "not_found", "任务不存在")
            if status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise ApiError(409, "contract_locked", "终态任务不能改合同")
            updated = conn.execute(
                text(
                    "UPDATE tasks SET draft_contract = CAST(:draft AS jsonb), version = version + 1, projection_updated_at = now() "
                    "WHERE project_id = :project_id AND id = :id AND version::text = :version"
                ),
                {"draft": json.dumps(draft), "project_id": project_id, "id": task_id, "version": match},
            ).rowcount
        if updated != 1:
            raise ApiError(409, "version_conflict", "合同版本已变化")
        return draft

    @app.post("/v1/projects/{project_id}/tasks/{task_id}/commands", status_code=202, operation_id="submitTaskCommand")
    def submit_command(project_id: str, task_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        _task_id(task_id)
        key = _idempotency_key(request)
        command_id, kind = _command_body(body)
        payload_hash = sha256_text(json.dumps({"command_id": command_id, "kind": kind}, sort_keys=True, separators=(",", ":")))
        route = f"POST /v1/projects/{project_id}/tasks/{task_id}/commands"
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            existing = _claim_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, payload_hash=payload_hash)
            if existing is not None:
                return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
            task = conn.execute(
                text("SELECT workflow_id, status FROM tasks WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": task_id},
            ).mappings().first()
            if task is None:
                raise ApiError(404, "not_found", "任务不存在")
            try:
                insert_followup_command(conn, project_id=project_id, task_id=task_id, workflow_id=task["workflow_id"], command_key=command_id, kind=kind)
            except IntegrityError as exc:
                raise ApiError(409, "command_exists", "命令已经接收") from exc
            if kind == "cancel":
                conn.execute(
                    text("UPDATE tasks SET resume_state = 'cancel_requested', version = version + 1 WHERE id = :id AND status <> 'CANCELLED'"),
                    {"id": task_id},
                )
            elif kind == "pause":
                conn.execute(
                    text(
                        "UPDATE tasks SET resume_state = 'pause_requested', version = version + 1 "
                        "WHERE id = :id AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')"
                    ),
                    {"id": task_id},
                )
            elif kind == "resume":
                conn.execute(
                    text(
                        "UPDATE tasks SET resume_state = NULL, version = version + 1 "
                        "WHERE id = :id AND resume_state IS DISTINCT FROM 'cancel_requested' "
                        "AND (status = 'PAUSED' OR resume_state = 'pause_requested')"
                    ),
                    {"id": task_id},
                )
            _append_event(conn, project_id=project_id, task_id=task_id, event_type="command.accepted", payload={"command_id": command_id, "kind": kind})
            accepted = _accepted(request, command_id, task_id)
            _store_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, body=accepted)
        return JSONResponse(status_code=202, content=accepted)

    @app.get("/v1/projects/{project_id}/tasks/{task_id}/events", operation_id="streamTaskEvents")
    def stream_events(project_id: str, task_id: str, request: Request) -> StreamingResponse:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _task_id(task_id)
        last_id = request.headers.get("last-event-id")
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            if conn.execute(text("SELECT 1 FROM tasks WHERE project_id = :project_id AND id = :id"), {"project_id": project_id, "id": task_id}).first() is None:
                raise ApiError(404, "not_found", "任务不存在")
            after = 0
            if last_id:
                found = conn.execute(
                    text("SELECT sequence FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_key = :event_key"),
                    {"project_id": project_id, "task_id": task_id, "event_key": last_id},
                ).scalar()
                if found is None:
                    raise ApiError(422, "cursor_expired", "事件游标已失效")
                after = int(found)
            rows = conn.execute(
                text(
                    "SELECT event_key, sequence, event_type, payload, created_at FROM task_events "
                    "WHERE project_id = :project_id AND task_id = :task_id AND sequence > :after ORDER BY sequence"
                ),
                {"project_id": project_id, "task_id": task_id, "after": after},
            ).mappings().all()
            pending = conn.execute(
                text(
                    "SELECT command_key FROM command_outbox WHERE project_id = :project_id AND task_id = :task_id "
                    "AND status IN ('PENDING', 'DELIVERING') ORDER BY created_at"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).scalars().all()
            watermark = conn.execute(
                text("SELECT projection_updated_at FROM tasks WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": task_id},
            ).scalar()
        body = _render_events(project_id, task_id, rows, [str(item) for item in pending], watermark)
        return StreamingResponse(iter([body]), media_type="text/event-stream")

    @app.get("/v1/projects/{project_id}/approvals", operation_id="listApprovals")
    def list_approvals(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        if limit < 1 or limit > 100:
            raise ApiError(422, "invalid_limit", "limit 必须在 1 到 100 之间")
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, task_id::text AS task_id, intent_digest, requested_payload, expires_at, decision, version, created_at "
                    "FROM approvals WHERE project_id = :project_id AND decision = 'pending' AND expires_at > now() "
                    "AND (CAST(:created_at AS timestamptz) IS NULL "
                    "OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
        items = [_approval(row) for row in rows]
        next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if len(items) == limit else None
        return {"items": items, "next_cursor": next_cursor}

    @app.post("/v1/projects/{project_id}/approvals/{approval_id}/decision", status_code=202, operation_id="decideApproval")
    def decide_approval(project_id: str, approval_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        _task_id(approval_id)
        key = _idempotency_key(request)
        decision, digest = _decision_body(body)
        match = request.headers.get("if-match")
        payload_hash = sha256_text(json.dumps({"decision": decision, "expected_intent_hash": digest}, sort_keys=True, separators=(",", ":")))
        route = f"POST /v1/projects/{project_id}/approvals/{approval_id}/decision"
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            existing = _claim_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, payload_hash=payload_hash)
            if existing is not None:
                return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
            row = conn.execute(
                text("SELECT task_id::text AS task_id, version FROM approvals WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": approval_id},
            ).mappings().first()
        if row is None:
            raise ApiError(404, "not_found", "审批不存在")
        if match != str(row["version"]):
            raise ApiError(409, "version_conflict", "审批版本已变化")
        try:
            OperationLedger(_db(app)).decide(
                owner_id=owner,
                project_id=project_id,
                task_id=row["task_id"],
                approval_id=approval_id,
                decision="approved" if decision == "approve" else "rejected",
                expected_intent_hash=digest,
            )
        except OperationError as exc:
            raise ApiError(409, exc.code, "审批不能接受") from exc
        accepted = _accepted(request, str(uuid.uuid4()), approval_id)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            _store_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, body=accepted)
        return JSONResponse(status_code=202, content=accepted)


def _db(app: FastAPI) -> Database:
    database = app.state.database
    if database is None:
        raise ApiError(503, "database_unconfigured", "数据库未配置")
    return database


def _owner(app: FastAPI, request: Request, *, csrf: bool = False, origin: Settings | None = None) -> str:
    if app.state.auth is None:
        raise ApiError(503, "database_unconfigured", "数据库未配置")
    if csrf:
        if origin is None or request.headers.get("origin") not in origin.origins:
            raise ApiError(403, "origin_rejected", "Origin 不在允许列表")
        session = app.state.auth.require_csrf(request.cookies.get("aether_session") or "", request.headers.get("x-csrf-token"))
        return session["owner_id"]
    token = request.cookies.get("aether_session")
    if not token:
        raise ApiError(401, "unauthenticated", "未登录")
    return app.state.auth.authenticate(token)["owner_id"]


def _owned(app: FastAPI, owner_id: str, project_id: str) -> None:
    try:
        uuid.UUID(project_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "项目不存在") from exc
    with _db(app).transaction(owner_id=owner_id) as conn:
        row = conn.execute(text("SELECT id FROM projects WHERE id = :id AND deleted_at IS NULL"), {"id": project_id}).first()
    if row is None:
        raise ApiError(404, "not_found", "项目不存在")


def _task_id(value: str) -> None:
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise ApiError(404, "not_found", "任务不存在") from exc


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if key is None or not 8 <= len(key) <= 128:
        raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key 长度必须在 8 到 128 之间")
    return key


def _claim_idempotency(conn, *, owner: str, project_id: str, route: str, key: str, payload_hash: str) -> dict[str, Any] | None:
    inserted = conn.execute(
        text(
            "INSERT INTO idempotency_keys (owner_id, project_id, route, idempotency_key, payload_sha256, response_status, response_body) "
            "VALUES (:owner, :project_id, :route, :key, :payload_hash, 0, '{}'::jsonb) "
            "ON CONFLICT (owner_id, project_id, route, idempotency_key) WHERE project_id IS NOT NULL DO NOTHING RETURNING id"
        ),
        {"owner": owner, "project_id": project_id, "route": route, "key": key, "payload_hash": payload_hash},
    ).first()
    if inserted is not None:
        return None
    existing = conn.execute(
        text(
            "SELECT payload_sha256, response_status, response_body FROM idempotency_keys "
            "WHERE owner_id = :owner AND project_id = :project_id AND route = :route AND idempotency_key = :key"
        ),
        {"owner": owner, "project_id": project_id, "route": route, "key": key},
    ).mappings().one()
    if existing["payload_sha256"] != payload_hash or existing["response_status"] == 0:
        raise ApiError(409, "idempotency_conflict", "同一个幂等键对应了不同内容或尚未完成")
    return dict(existing)


def _store_idempotency(conn, *, owner: str, project_id: str, route: str, key: str, body: dict[str, Any], status: int = 202) -> None:
    conn.execute(
        text(
            "UPDATE idempotency_keys SET response_status = :status, response_body = CAST(:body AS jsonb) "
            "WHERE owner_id = :owner AND project_id = :project_id AND route = :route AND idempotency_key = :key"
        ),
        {"owner": owner, "project_id": project_id, "route": route, "key": key, "body": json.dumps(body), "status": status},
    )


def _append_event(conn, *, project_id: str, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
    event_id = f"evt-{uuid.uuid4()}"
    conn.execute(
        text("SELECT id FROM tasks WHERE project_id = :project_id AND id = :task_id FOR UPDATE"),
        {"project_id": project_id, "task_id": task_id},
    )
    for _attempt in range(5):
        sequence = conn.execute(
            text("SELECT coalesce(max(sequence), 0) + 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id"),
            {"project_id": project_id, "task_id": task_id},
        ).scalar_one()
        try:
            with conn.begin_nested():
                _insert_event(conn, project_id=project_id, task_id=task_id, sequence=sequence, event_key=event_id, event_type=event_type, payload=payload)
            return
        except IntegrityError:
            continue
    raise ApiError(409, "event_conflict", "事件序号冲突")


def _insert_event(conn, *, project_id: str, task_id: str, sequence: int, event_key: str, event_type: str, payload: dict[str, Any]) -> None:
    conn.execute(
        text(
            "INSERT INTO task_events (project_id, task_id, sequence, event_key, event_type, payload) "
            "VALUES (:project_id, :task_id, :sequence, :event_key, :event_type, CAST(:payload AS jsonb))"
        ),
        {
            "project_id": project_id,
            "task_id": task_id,
            "sequence": sequence,
            "event_key": event_key,
            "event_type": event_type,
            "payload": json.dumps(payload),
        },
    )
    conn.execute(
        text("UPDATE tasks SET projection_updated_at = now(), version = version + 1 WHERE project_id = :project_id AND id = :id"),
        {"project_id": project_id, "id": task_id},
    )


def _snapshot(conn, project_id: str, task_id: str) -> dict[str, Any] | None:
    task = conn.execute(
        text(
            "SELECT t.id::text AS id, t.status, t.version, t.projection_updated_at, t.created_at, t.draft_contract, "
            "g.contract_sha256 AS authoritative_sha256 "
            "FROM tasks AS t "
            "LEFT JOIN goal_contracts AS g ON g.project_id = t.project_id AND g.task_id = t.id AND g.id = t.current_contract_id "
            "WHERE t.project_id = :project_id AND t.id = :id"
        ),
        {"project_id": project_id, "id": task_id},
    ).mappings().first()
    if task is None:
        return None
    budget = conn.execute(
        text(
            "SELECT limit_micro_usd, spent_micro_usd, held_micro_usd, tokens_used, requests_admitted "
            "FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id"
        ),
        {"project_id": project_id, "task_id": task_id},
    ).mappings().first()
    usage = conn.execute(
        text(
            "SELECT COUNT(*) FILTER (WHERE usage_status = 'unknown') AS unknown_request_count, "
            "COUNT(*) FILTER (WHERE usage_status = 'observed') AS observed_request_count, "
            "COALESCE(SUM(actual_micro_usd) FILTER (WHERE usage_status = 'observed'), 0) AS observed_micro_usd "
            "FROM model_requests WHERE project_id = :project_id AND task_id = :task_id"
        ),
        {"project_id": project_id, "task_id": task_id},
    ).mappings().one()
    artifacts = conn.execute(
        text(
            "SELECT id::text FROM ("
            "SELECT a.id, a.created_at FROM artifacts AS a "
            "WHERE a.project_id = :project_id AND a.task_id = :task_id AND a.deleted_at IS NULL "
            "UNION "
            "SELECT a.id, a.created_at FROM task_artifacts AS link "
            "JOIN artifacts AS a ON a.project_id = link.project_id AND a.id = link.artifact_id "
            "WHERE link.project_id = :project_id AND link.task_id = :task_id "
            "AND link.deleted_at IS NULL AND a.deleted_at IS NULL"
            ") AS visible ORDER BY created_at, id"
        ),
        {"project_id": project_id, "task_id": task_id},
    ).scalars().all()
    verifications = conn.execute(
        text(
            "SELECT id::text AS id, criterion_id, verdict, artifact_sha256, contract_sha256, verifier_identity, "
            "report_artifact_id::text AS report_artifact_id, reason, checked_at "
            "FROM verification_results WHERE project_id = :project_id AND task_id = :task_id ORDER BY created_at, id"
        ),
        {"project_id": project_id, "task_id": task_id},
    ).mappings().all()
    draft = task["draft_contract"] if isinstance(task["draft_contract"], dict) else {}
    authoritative = task["authoritative_sha256"]
    return {
        "id": task["id"],
        "project_id": project_id,
        "status": task["status"],
        "version": int(task["version"]),
        "contract_version": draft.get("contract_version"),
        "contract_sha256": authoritative or draft.get("contract_sha256"),
        "projection_updated_at": task["projection_updated_at"],
        "created_at": task["created_at"],
        "budget": budget,
        "unknown_request_count": int(usage["unknown_request_count"]),
        "observed_request_count": int(usage["observed_request_count"]),
        "observed_micro_usd": int(usage["observed_micro_usd"]),
        "artifact_ids": [str(item) for item in artifacts],
        "verification_results": [dict(row) for row in verifications],
    }


def _public_task(item: dict[str, Any] | None) -> dict[str, Any]:
    if item is None:
        raise ApiError(404, "not_found", "任务不存在")
    budget = item["budget"]
    return {
        "id": item["id"],
        "project_id": item["project_id"],
        "status": item["status"],
        "version": item["version"],
        "contract_version": item["contract_version"],
        "contract_sha256": item["contract_sha256"],
        "projection_updated_at": _stamp(item["projection_updated_at"]),
        "budget": {
            "limit_micro_usd": int(budget["limit_micro_usd"]) if budget else 0,
            "actual_known_micro_usd": int(budget["spent_micro_usd"]) if budget else 0,
            "held_micro_usd": int(budget["held_micro_usd"]) if budget else 0,
            "unknown_request_count": item["unknown_request_count"],
            "observed_request_count": item["observed_request_count"],
            "observed_micro_usd": item["observed_micro_usd"],
            "tokens_used": int(budget["tokens_used"]) if budget else 0,
            "request_count": int(budget["requests_admitted"]) if budget else 0,
        },
        "artifact_ids": item["artifact_ids"],
        "verification_results": [_verification(row) for row in item["verification_results"]],
    }


def _verification(row: dict[str, Any]) -> dict[str, Any]:
    evidence = str(row["report_artifact_id"] or row["id"])
    return {
        "criterion_id": row["criterion_id"],
        "verdict": row["verdict"],
        "artifact_sha256": row["artifact_sha256"],
        "contract_sha256": row["contract_sha256"],
        "verifier_identity": row["verifier_identity"],
        "evidence_ids": [evidence],
        "reason": row["reason"],
        "checked_at": _stamp(row["checked_at"]),
    }


def _approval(row: Any) -> dict[str, Any]:
    payload = row["requested_payload"] if isinstance(row["requested_payload"], dict) else {}
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "intent_digest": row["intent_digest"],
        "normalized_payload": payload,
        "risk": payload.get("risk", "R2"),
        "expires_at": _stamp(row["expires_at"]),
        "decision": row["decision"],
        "version": int(row["version"]),
    }


def _draft_contract(body: dict[str, Any], project_id: str, task_id: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_contract", "合同不合法")
    goal = body.get("goal")
    artifacts = body.get("required_artifacts")
    criteria = body.get("criteria")
    created_at = body.get("created_at")
    if body.get("project_id") != project_id or body.get("task_id") != task_id:
        raise ApiError(422, "invalid_contract", "合同不属于这个任务")
    if not isinstance(goal, str) or not goal.strip():
        raise ApiError(422, "invalid_contract", "合同缺少目标")
    if not isinstance(artifacts, list) or not artifacts or not all(isinstance(item, str) and item for item in artifacts):
        raise ApiError(422, "invalid_contract", "合同缺少产物")
    if not isinstance(criteria, list) or not criteria or not all(_criterion(item) for item in criteria):
        raise ApiError(422, "invalid_contract", "合同缺少验收条款")
    if not isinstance(created_at, str) or not created_at:
        raise ApiError(422, "invalid_contract", "合同缺少时间")
    version = body.get("contract_version")
    if not isinstance(version, int) or version < 1:
        raise ApiError(422, "invalid_contract", "合同版本不合法")
    return body


def _criterion(item: Any) -> bool:
    return isinstance(item, dict) and isinstance(item.get("criterion_id"), str) and item.get("template") in _TEMPLATES and isinstance(item.get("description"), str) and bool(item["description"])


def _command_body(body: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_command", "命令不合法")
    command_id = body.get("command_id")
    kind = body.get("kind")
    try:
        uuid.UUID(str(command_id))
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid_command", "命令编号不合法") from exc
    if kind not in _COMMANDS or set(body) - {"command_id", "kind", "expected_contract_sha256", "reason"}:
        raise ApiError(422, "invalid_command", "命令不合法")
    return str(command_id), str(kind)


def _decision_body(body: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_decision", "审批决定不合法")
    decision = body.get("decision")
    digest = body.get("expected_intent_hash")
    if decision not in {"approve", "reject"} or not isinstance(digest, str) or len(digest) != 64:
        raise ApiError(422, "invalid_decision", "审批决定不合法")
    if set(body) - {"decision", "expected_intent_hash", "reason"}:
        raise ApiError(422, "invalid_decision", "审批决定不合法")
    return decision, digest


def _budget_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"max_cost_micro_usd", "max_tokens", "max_model_requests", "active_seconds", "verification_reserve_fraction"}:
        raise ApiError(422, "invalid_budget", "任务预算不合法")
    spec = {
        "max_cost_micro_usd": 2_000_000,
        "max_tokens": 100_000,
        "max_model_requests": 48,
        "active_seconds": 1800,
        "verification_reserve_fraction": 0.2,
    }
    spec.update(value)
    if any(isinstance(spec[name], bool) or not isinstance(spec[name], int) for name in ("max_cost_micro_usd", "max_tokens", "max_model_requests", "active_seconds")):
        raise ApiError(422, "invalid_budget", "任务预算不合法")
    fraction = spec["verification_reserve_fraction"]
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise ApiError(422, "invalid_budget", "任务预算不合法")
    spec["verification_reserve_fraction"] = float(fraction)
    if spec["max_cost_micro_usd"] < 0 or spec["max_tokens"] < 1 or spec["max_model_requests"] < 1 or spec["active_seconds"] < 1:
        raise ApiError(422, "invalid_budget", "任务预算不合法")
    if not 0 <= spec["verification_reserve_fraction"] < 1:
        raise ApiError(422, "invalid_budget", "任务预算不合法")
    return spec


def _source_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ApiError(422, "invalid_sources", "允许的来源不合法")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ApiError(422, "invalid_sources", "允许的来源不合法")
        try:
            parsed.append(str(uuid.UUID(item)))
        except ValueError as exc:
            raise ApiError(422, "invalid_sources", "允许的来源不合法") from exc
    if len(parsed) != len(set(parsed)):
        raise ApiError(422, "invalid_sources", "允许的来源不合法")
    return parsed


def _accepted(request: Request, command_id: str, resource_id: str) -> dict[str, str]:
    return {"request_id": getattr(request.state, "request_id", ""), "command_id": command_id, "resource_id": resource_id, "status": "accepted"}


def _render_events(project_id: str, task_id: str, rows: list[Any], pending: list[str], watermark: datetime | None) -> str:
    parts = [
        "event: watermark\n"
        f"data: {json.dumps({'projection_updated_at': _stamp(watermark), 'pending_commands': pending}, ensure_ascii=False)}\n\n"
    ]
    for row in rows:
        envelope = {
            "schema_version": "1.0",
            "event_id": row["event_key"],
            "project_id": project_id,
            "task_id": task_id,
            "sequence": int(row["sequence"]),
            "event_type": row["event_type"],
            "occurred_at": _stamp(row["created_at"]),
            "payload": row["payload"] if isinstance(row["payload"], dict) else {},
        }
        parts.append(f"id: {row['event_key']}\nevent: task.event\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n")
    return "".join(parts)


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _encode_cursor(created_at: datetime, item_id: str) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{item_id}".encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if cursor is None:
        return None, None
    try:
        text_value = base64.urlsafe_b64decode(cursor.encode()).decode()
        created, item_id = text_value.split("|", 1)
        return datetime.fromisoformat(created), item_id
    except (ValueError, UnicodeError) as exc:
        raise ApiError(422, "invalid_cursor", "分页游标无效") from exc
