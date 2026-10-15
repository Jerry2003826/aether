"""Project console routes. Lists read the database. Mutations enqueue a command."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aether_api.control import (
    _accepted,
    _claim_idempotency,
    _db,
    _decode_cursor,
    _encode_cursor,
    _idempotency_key,
    _owned,
    _owner,
    _store_idempotency,
    _task_id,
)
from aether_api.errors import ApiError
from aether_api.memory_routes import _enqueue
from aether_api.settings import Settings
from aether_domain.security import sha256_text
from aether_worker.recommendations import rank_candidates

_ERRORS = {
    401: {"description": "未登录"},
    403: {"description": "无权访问"},
    404: {"description": "不存在"},
    409: {"description": "版本或幂等冲突"},
    422: {"description": "请求不合法"},
    429: {"description": "被限流"},
    503: {"description": "依赖不可用"},
}
_SOURCE_KINDS = {"url", "rss", "local_document", "github_api", "arxiv_api"}
_FEEDBACK_KINDS = {"click", "save", "helpful", "dwell", "hide", "unsave"}
_SCORE_KEYS = {"metrics", "score", "decision", "success_delta", "promoted"}


def register_console_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/v1/projects/{project_id}/sources", operation_id="listSources", responses=_ERRORS)
    def list_sources(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _limit(limit)
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, kind, locator, status, version, created_at FROM sources "
                    "WHERE project_id = :project_id AND deleted_at IS NULL "
                    "AND (CAST(:created_at AS timestamptz) IS NULL OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
            items = [_source_row(conn, project_id, row) for row in rows]
        return {"items": items, "next_cursor": _next(rows, items, limit)}

    @app.post("/v1/projects/{project_id}/sources", status_code=202, operation_id="createSource", responses=_ERRORS)
    def create_source(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        payload = _source_create_body(body)
        source_id = str(uuid.uuid4())
        command = {"source_id": source_id, "canonical_locator_hash": sha256_text(f"{payload['kind']}\n{payload['locator']}"), **payload}
        return _accept(app, settings, request, project_id, "source.create", payload, command, source_id)

    @app.get("/v1/projects/{project_id}/sources/{source_id}", operation_id="getSource", responses=_ERRORS)
    def get_source(project_id: str, source_id: str, request: Request) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _task_id(source_id)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT id::text AS id, kind, locator, status, version, created_at FROM sources "
                    "WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"
                ),
                {"project_id": project_id, "id": source_id},
            ).mappings().first()
            if row is None:
                raise ApiError(404, "not_found", "来源不存在")
            return _source_row(conn, project_id, row)

    @app.patch("/v1/projects/{project_id}/sources/{source_id}", status_code=202, operation_id="updateSource", responses=_ERRORS)
    def update_source(project_id: str, source_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        _task_id(source_id)
        patch = _source_patch_body(body)
        command = {"source_id": source_id, "expected_version": _match(request), **patch}
        return _accept(app, settings, request, project_id, "source.patch", {"source_id": source_id, **patch, "expected_version": command["expected_version"]}, command, source_id)

    @app.delete("/v1/projects/{project_id}/sources/{source_id}", status_code=202, operation_id="deleteSource", responses=_ERRORS)
    def delete_source(project_id: str, source_id: str, request: Request) -> JSONResponse:
        _task_id(source_id)
        command = {"source_id": source_id, "expected_version": _match(request)}
        return _accept(app, settings, request, project_id, "source.delete", command, command, source_id)

    @app.get("/v1/projects/{project_id}/ingestions", operation_id="listIngestions", responses=_ERRORS)
    def list_ingestions(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _limit(limit)
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, source_id::text AS source_id, status, source_version_id::text AS source_version_id, error_code, created_at "
                    "FROM ingestion_jobs WHERE project_id = :project_id "
                    "AND (CAST(:created_at AS timestamptz) IS NULL OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
        items = [
            {
                "id": row["id"],
                "source_id": row["source_id"],
                "status": row["status"],
                "source_version_id": row["source_version_id"],
                "error_code": row["error_code"],
            }
            for row in rows
        ]
        return {"items": items, "next_cursor": _next(rows, items, limit)}

    @app.post("/v1/projects/{project_id}/ingestions", status_code=202, operation_id="createIngestion", responses=_ERRORS)
    def create_ingestion(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        if set(body) != {"source_id"}:
            raise ApiError(422, "invalid_ingestion", "采集请求不合法")
        _task_id(str(body["source_id"]))
        ingestion_id = str(uuid.uuid4())
        command = {"ingestion_id": ingestion_id, "source_id": str(body["source_id"])}
        return _accept(app, settings, request, project_id, "ingestion.create", {"source_id": command["source_id"]}, command, ingestion_id)

    @app.post("/v1/projects/{project_id}/search", operation_id="searchKnowledge", responses=_ERRORS)
    def search_knowledge(project_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        owner = _owner(app, request, csrf=True, origin=settings)
        _owned(app, owner, project_id)
        _search_body(body, project_id)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            generation = conn.execute(
                text(
                    "SELECT id::text AS id, state FROM index_generations "
                    "WHERE project_id = :project_id AND state = 'ACTIVE' ORDER BY activated_at DESC NULLS LAST LIMIT 1"
                ),
                {"project_id": project_id},
            ).mappings().first()
        if generation is None:
            message = "项目还没有可用索引"
        else:
            message = "索引存在，但这次请求没有绑定的检索进程"
        raise ApiError(503, "index_unavailable", message, retryable=True)

    @app.get("/v1/projects/{project_id}/recommendations", operation_id="getRecommendations", responses=_ERRORS)
    def get_recommendations(project_id: str, request: Request) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text("SELECT id::text AS id, locator FROM sources WHERE project_id = :project_id AND deleted_at IS NULL ORDER BY created_at, id"),
                {"project_id": project_id},
            ).mappings().all()
        titles = {row["id"]: row["locator"] for row in rows}
        candidates = [{"source_id": row["id"], "goal_match": 1.0, "deleted": False} for row in rows]
        ranked = rank_candidates(_db(app), owner_id=owner, project_id=project_id, candidates=candidates, explore=False)
        items = [
            {
                "source_id": item["source_id"],
                "title": titles.get(item["source_id"], item["source_id"]),
                "score": float(item["goal_match"]),
                "reasons": [str(item["reason"])],
                "exploration": False,
                "propensity": 1.0,
            }
            for item in ranked["items"]
        ]
        return {
            "impression_id": ranked["impression_id"],
            "profile_version": int(ranked["profile_version"]),
            "policy_version": ranked["policy_version"],
            "items": items,
        }

    @app.post("/v1/projects/{project_id}/feedback", status_code=202, operation_id="recordFeedback", responses=_ERRORS)
    def record_feedback(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        command = _feedback_body(body)
        return _accept(app, settings, request, project_id, "feedback.record", command, command, command["event_id"])

    @app.get("/v1/projects/{project_id}/skills", operation_id="listSkills", responses=_ERRORS)
    def list_skills(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _limit(limit)
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, name, description, created_at FROM skills "
                    "WHERE project_id = :project_id AND deleted_at IS NULL "
                    "AND (CAST(:created_at AS timestamptz) IS NULL OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
            items = []
            for row in rows:
                versions = conn.execute(
                    text("SELECT manifest FROM skill_versions WHERE project_id = :project_id AND skill_id = :skill_id ORDER BY version"),
                    {"project_id": project_id, "skill_id": row["id"]},
                ).scalars().all()
                items.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "description": row["description"],
                        "versions": [_manifest(item) for item in versions],
                    }
                )
        return {"items": items, "next_cursor": _next(rows, items, limit)}

    @app.post("/v1/projects/{project_id}/skills", status_code=202, operation_id="createSkill", responses=_ERRORS)
    def create_skill(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        if set(body) != {"bundle_artifact_id"}:
            raise ApiError(422, "invalid_skill", "技能导入不合法")
        _task_id(str(body["bundle_artifact_id"]))
        skill_id = str(uuid.uuid4())
        command = {
            "skill_id": skill_id,
            "version_id": str(uuid.uuid4()),
            "bundle_artifact_id": str(body["bundle_artifact_id"]),
            "name": "q" + uuid.uuid4().hex[:8],
        }
        return _accept(app, settings, request, project_id, "skill.import", {"bundle_artifact_id": command["bundle_artifact_id"]}, command, skill_id)

    @app.post("/v1/projects/{project_id}/skills/{skill_id}/evaluate", status_code=202, operation_id="evaluateSkill", responses=_ERRORS)
    def evaluate_skill(project_id: str, skill_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        _task_id(skill_id)
        stable = _evaluate_body(body)
        command = {**stable, "skill_id": skill_id, "evaluation_id": str(uuid.uuid4())}
        return _accept(app, settings, request, project_id, "skill.evaluate", {**stable, "skill_id": skill_id}, command, command["evaluation_id"])

    @app.post("/v1/projects/{project_id}/skills/{skill_id}/deployments", status_code=202, operation_id="deploySkill", responses=_ERRORS)
    def deploy_skill(project_id: str, skill_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        _task_id(skill_id)
        stable = _deploy_body(body)
        command = {**stable, "skill_id": skill_id, "expected_deployment_version": _match(request)}
        return _accept(app, settings, request, project_id, "skill.deploy", command, command, skill_id)

    @app.post("/v1/projects/{project_id}/coding-jobs", status_code=202, operation_id="createCodingJob", responses=_ERRORS)
    def create_coding_job(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        stable = _coding_body(body)
        command = {
            **stable,
            "coding_job_id": str(uuid.uuid4()),
            "operation_id": str(uuid.uuid4()),
            "capability_id": str(uuid.uuid4()),
        }
        return _accept(app, settings, request, project_id, "coding.create", stable, command, command["coding_job_id"])

    @app.get("/v1/projects/{project_id}/watches", operation_id="listWatches", responses=_ERRORS)
    def list_watches(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _limit(limit)
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, purpose, source_ids, schedule_spec, status, version, last_run_at, created_at FROM watches "
                    "WHERE project_id = :project_id AND status <> 'DELETED' "
                    "AND (CAST(:created_at AS timestamptz) IS NULL OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
        items = [_watch(row) for row in rows]
        return {"items": items, "next_cursor": _next(rows, items, limit)}

    @app.post("/v1/projects/{project_id}/watches", status_code=202, operation_id="createWatch", responses=_ERRORS)
    def create_watch(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        watch_id = str(uuid.uuid4())
        stable = _watch_create_body(body)
        command = {"watch_id": watch_id, "schedule_id": f"aether-watch-{watch_id}", **stable}
        return _accept(app, settings, request, project_id, "watch.create", stable, command, watch_id)

    @app.patch("/v1/projects/{project_id}/watches/{watch_id}", status_code=202, operation_id="updateWatch", responses=_ERRORS)
    def update_watch(project_id: str, watch_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        _task_id(watch_id)
        stable = {"watch_id": watch_id, "expected_version": _match(request), **_watch_patch_body(body)}
        return _accept(app, settings, request, project_id, "watch.patch", stable, stable, watch_id)

    @app.delete("/v1/projects/{project_id}/watches/{watch_id}", status_code=202, operation_id="deleteWatch", responses=_ERRORS)
    def delete_watch(project_id: str, watch_id: str, request: Request) -> JSONResponse:
        _task_id(watch_id)
        command = {"watch_id": watch_id, "expected_version": _match(request)}
        return _accept(app, settings, request, project_id, "watch.delete", command, command, watch_id)

    @app.get("/v1/projects/{project_id}/evaluations", operation_id="listEvaluations", responses=_ERRORS)
    def list_evaluations(project_id: str, request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        _limit(limit)
        created_at, last_id = _decode_cursor(cursor)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, purpose, status, dataset_manifest_sha256, configuration, metrics, "
                    "report_artifact_id::text AS report_artifact_id, created_at FROM evaluation_runs "
                    "WHERE project_id = :project_id "
                    "AND (CAST(:created_at AS timestamptz) IS NULL OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"project_id": project_id, "created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
        items = [_evaluation(row) for row in rows]
        return {"items": items, "next_cursor": _next(rows, items, limit)}

    @app.post("/v1/projects/{project_id}/evaluations", status_code=202, operation_id="createEvaluation", responses=_ERRORS)
    def create_evaluation(project_id: str, request: Request, body: dict[str, Any]) -> JSONResponse:
        stable = _evaluation_body(body)
        command = {**stable, "evaluation_id": str(uuid.uuid4())}
        return _accept(app, settings, request, project_id, "evaluation.create", stable, command, command["evaluation_id"])

    @app.get("/v1/projects/{project_id}/costs", operation_id="getCosts", responses=_ERRORS)
    def get_costs(
        project_id: str,
        request: Request,
        range_from: str | None = Query(default=None, alias="from"),
        to: str | None = None,
    ) -> dict[str, Any]:
        owner = _owner(app, request)
        _owned(app, owner, project_id)
        start = _time(range_from)
        end = _time(to)
        with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
            account = conn.execute(
                text(
                    "SELECT COALESCE(SUM(limit_micro_usd), 0) AS limit_micro_usd, COALESCE(SUM(spent_micro_usd), 0) AS spent, "
                    "COALESCE(SUM(held_micro_usd), 0) AS held, COALESCE(SUM(tokens_used), 0) AS tokens_used "
                    "FROM budget_accounts WHERE project_id = :project_id"
                ),
                {"project_id": project_id},
            ).mappings().one()
            requests = conn.execute(
                text(
                    "SELECT COUNT(*) AS request_count, "
                    "COUNT(*) FILTER (WHERE usage_status = 'unknown') AS unknown_request_count, "
                    "COALESCE(SUM(actual_micro_usd) FILTER (WHERE usage_status = 'known'), 0) AS known "
                    "FROM model_requests WHERE project_id = :project_id "
                    "AND (CAST(:start AS timestamptz) IS NULL OR created_at >= CAST(:start AS timestamptz)) "
                    "AND (CAST(:end AS timestamptz) IS NULL OR created_at < CAST(:end AS timestamptz))"
                ),
                {"project_id": project_id, "start": start, "end": end},
            ).mappings().one()
        return {
            "limit_micro_usd": int(account["limit_micro_usd"]),
            "actual_known_micro_usd": int(requests["known"]),
            "held_micro_usd": int(account["held"]),
            "unknown_request_count": int(requests["unknown_request_count"]),
            "tokens_used": int(account["tokens_used"]),
            "request_count": int(requests["request_count"]),
        }

    @app.get("/v1/admin/connectors", operation_id="listConnectors", responses=_ERRORS)
    def list_connectors(request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        owner = _owner(app, request)
        _limit(limit)
        created_at, last_id = _decode_cursor(cursor)
        collected: list[dict[str, Any]] = []
        with _db(app).transaction(owner_id=owner) as conn:
            projects = conn.execute(text("SELECT id::text AS id FROM projects WHERE deleted_at IS NULL ORDER BY created_at, id")).scalars().all()
        for project_id in projects:
            with _db(app).transaction(owner_id=owner, project_id=str(project_id)) as conn:
                rows = conn.execute(
                    text(
                        "SELECT id::text AS id, project_id::text AS project_id, connector_key, transport, status, manifest_sha256, "
                        "secret_reference IS NOT NULL AS secret_configured, created_at FROM connector_configs ORDER BY created_at, id"
                    )
                ).mappings().all()
            for row in rows:
                public = _connector_public(row)
                public["created_at"] = row["created_at"]
                collected.append(public)
        collected.sort(key=lambda item: (item["created_at"], item["id"]))
        visible = [
            item
            for item in collected
            if created_at is None or (item["created_at"], item["id"]) > (created_at, last_id)
        ]
        page = visible[:limit]
        next_cursor = _encode_cursor(page[-1]["created_at"], page[-1]["id"]) if len(visible) > limit else None
        return {"items": [{key: value for key, value in item.items() if key != "created_at"} for item in page], "next_cursor": next_cursor}

    @app.post("/v1/admin/connectors", status_code=202, operation_id="registerConnector", responses=_ERRORS)
    def register_connector(request: Request, body: dict[str, Any]) -> JSONResponse:
        command = _connector_body(body)
        stable = {key: value for key, value in command.items() if key != "connector_id"}
        return _accept(app, settings, request, command["project_id"], "connector.register", stable, command, command["connector_id"])

    @app.post("/v1/admin/connectors/{connector_id}/probe", operation_id="probeConnector", responses=_ERRORS)
    def probe_connector(connector_id: str, request: Request) -> dict[str, Any]:
        owner = _owner(app, request, csrf=True, origin=settings)
        _task_id(connector_id)
        _idempotency_key(request)
        found = _find_connector(app, owner, connector_id)
        if found is None:
            raise ApiError(404, "not_found", "连接器不存在")
        return {
            "status": "unavailable",
            "checks": [{"check": "transport_not_opened", "ok": True}, {"check": "scope_unchanged", "ok": True}],
            "cost": _zero_cost(),
        }

    @app.post("/v1/admin/models/probe", operation_id="probeModel", responses=_ERRORS)
    def probe_model(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        _owner(app, request, csrf=True, origin=settings)
        _idempotency_key(request)
        _model_probe_body(body)
        key_present = bool(os.environ.get("AETHER_LIVE_MODEL_KEY"))
        return {
            "status": "unavailable",
            "checks": [{"check": "live_model", "result": "configured" if key_present else "missing", "passed": False}],
            "cost": _zero_cost(),
        }


def _accept(
    app: FastAPI,
    settings: Settings,
    request: Request,
    project_id: str,
    kind: str,
    stable: dict[str, Any],
    command: dict[str, Any],
    resource_id: str,
) -> JSONResponse:
    owner = _owner(app, request, csrf=True, origin=settings)
    _owned(app, owner, project_id)
    key = _idempotency_key(request)
    command_id = str(uuid.uuid4())
    command = {"command_id": command_id, **command}
    route = f"{request.method} {request.url.path}"
    digest = sha256_text(json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")))
    with _db(app).transaction(owner_id=owner, project_id=project_id) as conn:
        existing = _claim_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, payload_hash=digest)
        if existing is not None:
            return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
        _enqueue(conn, project_id=project_id, command_key=command_id, kind=kind, payload=command)
        accepted = _accepted(request, command_id, resource_id)
        _store_idempotency(conn, owner=owner, project_id=project_id, route=route, key=key, body=accepted)
    return JSONResponse(status_code=202, content=accepted)


def _limit(limit: int) -> None:
    if limit < 1 or limit > 100:
        raise ApiError(422, "invalid_limit", "limit 必须在 1 到 100 之间")


def _match(request: Request) -> str:
    match = request.headers.get("if-match")
    if match is None or not match.isdigit():
        raise ApiError(409, "version_conflict", "缺少版本")
    return match


def _next(rows: list[Any], items: list[Any], limit: int) -> str | None:
    if len(items) < limit or not rows:
        return None
    return _encode_cursor(rows[-1]["created_at"], rows[-1]["id"])


def _time(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(422, "invalid_range", "时间范围不合法") from exc
    return value


def _source_create_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) - {"kind", "locator", "allowed_domains", "fetch_interval_seconds"}:
        raise ApiError(422, "invalid_source", "来源不合法")
    kind = body.get("kind")
    locator = body.get("locator")
    domains = body.get("allowed_domains") or []
    interval = body.get("fetch_interval_seconds", 3600)
    if kind not in _SOURCE_KINDS or not isinstance(locator, str) or not locator.strip() or len(locator) > 4000:
        raise ApiError(422, "invalid_source", "来源不合法")
    if not isinstance(domains, list) or any(not isinstance(item, str) or not item for item in domains):
        raise ApiError(422, "invalid_source", "域名不合法")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 60:
        raise ApiError(422, "invalid_source", "抓取间隔不合法")
    return {"kind": kind, "locator": locator, "allowed_domains": domains, "fetch_interval_seconds": interval}


def _source_patch_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or not body or set(body) - {"allowed_domains", "fetch_interval_seconds", "status"}:
        raise ApiError(422, "invalid_source", "来源更新不合法")
    parsed = _source_create_body(
        {
            "kind": "url",
            "locator": "placeholder",
            "allowed_domains": body.get("allowed_domains", []),
            "fetch_interval_seconds": body.get("fetch_interval_seconds", 3600),
        }
    )
    result: dict[str, Any] = {}
    if "allowed_domains" in body:
        result["allowed_domains"] = parsed["allowed_domains"]
    if "fetch_interval_seconds" in body:
        result["fetch_interval_seconds"] = parsed["fetch_interval_seconds"]
    if "status" in body:
        if body["status"] not in {"ACTIVE", "PAUSED"}:
            raise ApiError(422, "invalid_source", "来源状态不合法")
        result["status"] = body["status"]
    return result


def _source_row(conn: Any, project_id: str, row: Any) -> dict[str, Any]:
    latest = conn.execute(
        text(
            "SELECT id::text FROM source_versions WHERE project_id = :project_id AND source_id = :source_id "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"project_id": project_id, "source_id": row["id"]},
    ).scalar()
    active = conn.execute(
        text("SELECT 1 FROM index_generations WHERE project_id = :project_id AND state = 'ACTIVE'"),
        {"project_id": project_id},
    ).first()
    if latest is None:
        index_status = "not_indexed"
    elif active is None:
        index_status = "pending"
    else:
        index_status = "indexed"
    return {
        "id": row["id"],
        "kind": row["kind"],
        "locator": row["locator"],
        "status": row["status"],
        "version": int(row["version"]),
        "latest_source_version_id": latest,
        "index_status": index_status,
    }


def _search_body(body: dict[str, Any], project_id: str) -> None:
    if not isinstance(body, dict) or set(body) - {"project_id", "query", "mode", "limit", "source_ids"}:
        raise ApiError(422, "invalid_search", "检索请求不合法")
    query = body.get("query")
    mode = body.get("mode", "hybrid")
    if body.get("project_id") != project_id or not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise ApiError(422, "invalid_search", "检索请求不合法")
    if mode not in {"lexical", "dense", "hybrid"}:
        raise ApiError(422, "invalid_search", "检索模式不合法")


def _feedback_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or "user" in body or set(body) - {"event_id", "impression_id", "source_id", "kind", "dwell_seconds", "event_time"}:
        raise ApiError(422, "invalid_feedback", "反馈不合法")
    _task_id(str(body.get("event_id")))
    _task_id(str(body.get("source_id")))
    if body.get("impression_id") is not None:
        _task_id(str(body["impression_id"]))
    if body.get("kind") not in _FEEDBACK_KINDS or not isinstance(body.get("event_time"), str):
        raise ApiError(422, "invalid_feedback", "反馈不合法")
    _time(str(body["event_time"]))
    dwell = body.get("dwell_seconds")
    if dwell is not None and (isinstance(dwell, bool) or not isinstance(dwell, (int, float)) or dwell < 0):
        raise ApiError(422, "invalid_feedback", "停留时间不合法")
    return {
        "event_id": str(body["event_id"]),
        "impression_id": None if body.get("impression_id") is None else str(body["impression_id"]),
        "source_id": str(body["source_id"]),
        "kind": body["kind"],
        "dwell_seconds": dwell,
        "event_time": body["event_time"],
    }


def _manifest(value: Any) -> dict[str, Any]:
    manifest = value if isinstance(value, dict) else json.loads(value)
    return {
        "name": manifest["name"],
        "version": int(manifest["version"]),
        "project_id": manifest["project_id"],
        "bundle_sha256": manifest["bundle_sha256"],
        "training_trace_ids": list(manifest.get("training_trace_ids") or []),
        "required_tool_ids": list(manifest.get("required_tool_ids") or []),
        "required_risk": manifest.get("required_risk", "R1"),
        "applicable_task_types": list(manifest.get("applicable_task_types") or ["research"]),
        "status": manifest["status"],
    }


def _evaluate_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or _SCORE_KEYS & set(body) or set(body) - {"candidate_version_id", "baseline_version_id", "dataset_manifest_id", "budget"}:
        raise ApiError(422, "invalid_evaluation", "评测请求不能自带分数")
    _task_id(str(body.get("candidate_version_id")))
    _task_id(str(body.get("dataset_manifest_id")))
    if body.get("baseline_version_id") is not None:
        _task_id(str(body["baseline_version_id"]))
    if not isinstance(body.get("budget"), dict):
        raise ApiError(422, "invalid_evaluation", "评测预算不合法")
    return {
        "candidate_version_id": str(body["candidate_version_id"]),
        "baseline_version_id": None if body.get("baseline_version_id") is None else str(body["baseline_version_id"]),
        "dataset_manifest_id": str(body["dataset_manifest_id"]),
        "budget": body["budget"],
        "configuration": {"kind": "skill.evaluate"},
    }


def _deploy_body(body: dict[str, Any]) -> dict[str, Any]:
    required = {"action", "target_version_id", "evaluation_id", "expected_previous_version_id", "approval_id", "report_sha256"}
    if not isinstance(body, dict) or set(body) != required:
        raise ApiError(422, "invalid_deploy", "部署请求不合法")
    if body["action"] not in {"promote", "rollback"}:
        raise ApiError(422, "invalid_deploy", "部署动作不合法")
    for key in ("target_version_id", "evaluation_id", "approval_id"):
        _task_id(str(body[key]))
    if body["expected_previous_version_id"] is not None:
        _task_id(str(body["expected_previous_version_id"]))
    digest = body["report_sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise ApiError(422, "invalid_deploy", "报告哈希不合法")
    return {
        "action": body["action"],
        "target_version_id": str(body["target_version_id"]),
        "evaluation_id": str(body["evaluation_id"]),
        "expected_previous_version_id": body["expected_previous_version_id"],
        "approval_id": str(body["approval_id"]),
        "report_sha256": digest,
    }


def _coding_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != {"task_id", "base_artifact_id", "job_key", "objective"}:
        raise ApiError(422, "invalid_coding_job", "编码作业不合法")
    _task_id(str(body["task_id"]))
    _task_id(str(body["base_artifact_id"]))
    job_key = body["job_key"]
    objective = body["objective"]
    if not isinstance(job_key, str) or not job_key.strip() or len(job_key) > 128:
        raise ApiError(422, "invalid_coding_job", "作业键不合法")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        raise ApiError(422, "invalid_coding_job", "目标不合法")
    return {
        "task_id": str(body["task_id"]),
        "base_artifact_id": str(body["base_artifact_id"]),
        "job_key": job_key.strip(),
        "objective": objective.strip(),
    }


def _watch_create_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != {"purpose", "source_ids", "schedule", "budget"}:
        raise ApiError(422, "invalid_watch", "跟踪请求不合法")
    purpose = body["purpose"]
    sources = body["source_ids"]
    schedule = body["schedule"]
    if not isinstance(purpose, str) or not purpose.strip() or not isinstance(sources, list) or not sources:
        raise ApiError(422, "invalid_watch", "跟踪请求不合法")
    for source_id in sources:
        _task_id(str(source_id))
    if not isinstance(schedule, dict) or set(schedule) - {"interval_seconds", "timezone"} or not isinstance(schedule.get("interval_seconds"), int):
        raise ApiError(422, "invalid_watch", "跟踪计划不合法")
    if isinstance(schedule["interval_seconds"], bool) or schedule["interval_seconds"] < 300:
        raise ApiError(422, "invalid_watch", "跟踪间隔不合法")
    if not isinstance(body["budget"], dict):
        raise ApiError(422, "invalid_watch", "跟踪预算不合法")
    return {
        "purpose": purpose,
        "source_ids": [str(item) for item in sources],
        "schedule": {"interval_seconds": schedule["interval_seconds"], "timezone": schedule.get("timezone", "Australia/Melbourne")},
        "budget": body["budget"],
    }


def _watch_patch_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or not body or set(body) - {"status", "interval_seconds", "expected_source_ids"}:
        raise ApiError(422, "invalid_watch", "跟踪更新不合法")
    result: dict[str, Any] = {}
    if "status" in body:
        if body["status"] not in {"ACTIVE", "PAUSED"}:
            raise ApiError(422, "invalid_watch", "跟踪状态不合法")
        result["status"] = body["status"]
    if "interval_seconds" in body:
        if isinstance(body["interval_seconds"], bool) or not isinstance(body["interval_seconds"], int) or body["interval_seconds"] < 300:
            raise ApiError(422, "invalid_watch", "跟踪间隔不合法")
        result["interval_seconds"] = body["interval_seconds"]
    if "expected_source_ids" in body:
        if not isinstance(body["expected_source_ids"], list):
            raise ApiError(422, "invalid_watch", "来源列表不合法")
        result["expected_source_ids"] = [str(item) for item in body["expected_source_ids"]]
    return result


def _watch(row: Any) -> dict[str, Any]:
    sources = row["source_ids"] if isinstance(row["source_ids"], list) else json.loads(row["source_ids"])
    schedule = row["schedule_spec"] if isinstance(row["schedule_spec"], dict) else json.loads(row["schedule_spec"])
    return {
        "id": row["id"],
        "purpose": row["purpose"],
        "source_ids": sources,
        "schedule": schedule,
        "status": row["status"],
        "version": int(row["version"]),
        "last_run_at": None if row["last_run_at"] is None else row["last_run_at"].isoformat().replace("+00:00", "Z"),
    }


def _evaluation_body(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or _SCORE_KEYS & set(body) or set(body) != {"purpose", "dataset_manifest_id", "configuration", "budget"}:
        raise ApiError(422, "invalid_evaluation", "评测请求不能自带分数")
    if body["purpose"] not in {"development", "validation", "release"} or not isinstance(body["configuration"], dict) or not isinstance(body["budget"], dict):
        raise ApiError(422, "invalid_evaluation", "评测请求不合法")
    _task_id(str(body["dataset_manifest_id"]))
    return {
        "purpose": body["purpose"],
        "dataset_manifest_id": str(body["dataset_manifest_id"]),
        "configuration": body["configuration"],
        "budget": body["budget"],
    }


def _evaluation(row: Any) -> dict[str, Any]:
    configuration = row["configuration"] if isinstance(row["configuration"], dict) else json.loads(row["configuration"])
    metrics = row["metrics"]
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    return {
        "id": row["id"],
        "purpose": row["purpose"],
        "status": row["status"],
        "dataset_manifest_sha256": row["dataset_manifest_sha256"],
        "configuration": configuration,
        "metrics": metrics,
        "report_artifact_id": row["report_artifact_id"],
    }


def _connector_body(body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"project_id", "connector_key", "transport", "manifest", "secret_value"}
    if not isinstance(body, dict) or set(body) - allowed or set(body) & {"project_id", "connector_key", "transport", "manifest"} != {"project_id", "connector_key", "transport", "manifest"}:
        raise ApiError(422, "invalid_connector", "连接器不合法")
    _task_id(str(body["project_id"]))
    if body["transport"] not in {"stdio", "streamable_http"} or not isinstance(body["connector_key"], str) or not body["connector_key"].strip():
        raise ApiError(422, "invalid_connector", "连接器不合法")
    if not isinstance(body["manifest"], dict):
        raise ApiError(422, "invalid_connector", "清单不合法")
    secret = body.get("secret_value")
    if secret is not None and not isinstance(secret, str):
        raise ApiError(422, "invalid_connector", "密钥不合法")
    manifest = {key: value for key, value in body["manifest"].items() if key != "secret_value"}
    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "connector_id": str(uuid.uuid4()),
        "project_id": str(body["project_id"]),
        "connector_key": body["connector_key"].strip(),
        "transport": body["transport"],
        "manifest": manifest,
        "manifest_sha256": sha256_text(raw),
        "secret_reference": None if not secret else "sha256:" + sha256_text(secret),
    }


def _connector_public(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "connector_key": row["connector_key"],
        "transport": row["transport"],
        "review_status": row["status"],
        "manifest_sha256": row["manifest_sha256"],
        "secret_configured": bool(row["secret_configured"]),
    }


def _find_connector(app: FastAPI, owner: str, connector_id: str) -> dict[str, Any] | None:
    with _db(app).transaction(owner_id=owner) as conn:
        projects = conn.execute(text("SELECT id::text FROM projects WHERE deleted_at IS NULL")).scalars().all()
    for project_id in projects:
        with _db(app).transaction(owner_id=owner, project_id=str(project_id)) as conn:
            row = conn.execute(
                text("SELECT id::text AS id, manifest_sha256, status FROM connector_configs WHERE id = :id"),
                {"id": connector_id},
            ).mappings().first()
        if row is not None:
            return dict(row)
    return None


def _model_probe_body(body: dict[str, Any]) -> None:
    if not isinstance(body, dict) or set(body) != {"alias", "allow_billable_probe", "max_cost_micro_usd"}:
        raise ApiError(422, "invalid_probe", "模型探针不合法")
    if not isinstance(body["alias"], str) or not body["alias"].strip() or body["allow_billable_probe"] is not True:
        raise ApiError(422, "invalid_probe", "模型探针不合法")
    if isinstance(body["max_cost_micro_usd"], bool) or not isinstance(body["max_cost_micro_usd"], int) or body["max_cost_micro_usd"] < 1:
        raise ApiError(422, "invalid_probe", "费用上限不合法")


def _zero_cost() -> dict[str, int]:
    return {
        "limit_micro_usd": 0,
        "actual_known_micro_usd": 0,
        "held_micro_usd": 0,
        "unknown_request_count": 0,
        "tokens_used": 0,
        "request_count": 0,
    }
