"""HTTP control plane. Unauthenticated and cross-project requests fail closed."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aether_api.auth_service import AuthService
from aether_api.console_routes import register_console_routes
from aether_api.control import register_control_routes
from aether_api.memory_routes import register_memory_routes
from aether_api.errors import ApiError
from aether_api.health import ready_payload
from aether_api.settings import Settings
from aether_api.throttle import LoginThrottle, client_address
from aether_domain.security import sha256_text
from aether_storage.artifacts import MAX_UPLOAD_BYTES, ArtifactError, ArtifactStore, read_bounded_upload
from aether_storage.db import Database

COOKIE = "aether_session"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    app = FastAPI(title="AETHER", version="1.4.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(resolved.origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "If-Match", "Last-Event-ID"],
    )
    app.state.settings = resolved
    app.state.database = Database(resolved.database_url) if resolved.database_url else None
    app.state.auth = AuthService(app.state.database, session_hours=resolved.session_hours, idle_minutes=resolved.idle_minutes) if app.state.database else None
    app.state.login_throttle = LoginThrottle(limit=resolved.login_failure_limit, window=timedelta(seconds=resolved.login_window_seconds))

    @app.middleware("http")
    async def request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = uuid.uuid4().hex
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=_error(request, exc.code, exc.message, exc.retryable, exc.details))

    @app.get("/health/live", operation_id="liveness")
    def live() -> dict[str, Any]:
        return {"status": "ok", "dependencies": {}}

    @app.get("/health/ready", operation_id="readiness")
    def ready() -> JSONResponse:
        return ready_payload()

    @app.post("/v1/auth/bootstrap", status_code=201, operation_id="bootstrapOwner")
    def bootstrap(request: Request, body: dict[str, Any]) -> JSONResponse:
        _require_loopback(request, resolved)
        _require_origin(request, resolved)
        _require_fields(body, "bootstrap_secret", "display_name", "passphrase")
        if app.state.auth is None:
            raise ApiError(503, "database_unconfigured", "数据库未配置")
        session = app.state.auth.bootstrap(resolved.bootstrap_path, str(body["bootstrap_secret"]), str(body["display_name"]), str(body["passphrase"]))
        return _session_response(session, resolved, status=201)

    @app.post("/v1/auth/session", operation_id="login")
    def login(request: Request, body: dict[str, Any]) -> JSONResponse:
        _require_origin(request, resolved)
        _require_fields(body, "passphrase")
        if app.state.auth is None:
            raise ApiError(503, "database_unconfigured", "数据库未配置")
        host = client_address(_client_host(request), request.headers.get("x-forwarded-for"), trust_forwarded_for=resolved.trust_forwarded_for)
        now = datetime.now(UTC)
        if not app.state.login_throttle.acquire(host, now):
            raise ApiError(429, "rate_limited", "登录尝试过多", retryable=True)
        try:
            session = app.state.auth.login(str(body["passphrase"]))
        except ApiError as exc:
            if exc.status != 401:
                app.state.login_throttle.release(host)
            raise
        app.state.login_throttle.release(host)
        return _session_response(session, resolved, status=200)

    @app.get("/v1/auth/session", operation_id="getSession")
    def get_session(request: Request) -> JSONResponse:
        session = _service(app).current(_cookie(request))
        return _session_response(session, resolved, status=200)

    @app.delete("/v1/auth/session", status_code=204, operation_id="logout")
    def logout(request: Request) -> Response:
        _require_origin(request, resolved)
        _service(app).logout(_cookie(request), request.headers.get("x-csrf-token"))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/v1/projects", operation_id="listProjects")
    def list_projects(request: Request, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ApiError(422, "invalid_limit", "limit 必须在 1 到 100 之间")
        owner = _service(app).authenticate(_cookie(request))
        created_at, last_id = _decode_cursor(cursor)
        with app.state.database.transaction(owner_id=owner["owner_id"]) as conn:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, name, version, visibility_epoch, created_at FROM projects "
                    "WHERE deleted_at IS NULL AND (CAST(:created_at AS timestamptz) IS NULL "
                    "OR (created_at, id) > (CAST(:created_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"created_at": created_at, "last_id": last_id, "limit": limit},
            ).mappings().all()
        items = [_project(row) for row in rows]
        next_cursor = None
        if len(items) == limit:
            next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"])
        return {"items": items, "next_cursor": next_cursor}

    @app.post("/v1/projects", status_code=201, operation_id="createProject")
    def create_project(request: Request, body: dict[str, Any]) -> JSONResponse:
        _require_origin(request, resolved)
        owner = _service(app).require_csrf(_cookie(request), request.headers.get("x-csrf-token"))
        key = request.headers.get("idempotency-key")
        if key is None or not 8 <= len(key) <= 128:
            raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key 长度必须在 8 到 128 之间")
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or set(body) - {"name"}:
            raise ApiError(422, "invalid_project", "项目名称不合法")
        payload_hash = sha256_text(json.dumps({"name": name}, sort_keys=True, separators=(",", ":")))
        with app.state.database.transaction(owner_id=owner["owner_id"]) as conn:
            inserted = conn.execute(
                text(
                    "INSERT INTO idempotency_keys (owner_id, route, idempotency_key, payload_sha256, response_status, response_body) "
                    "VALUES (:owner, 'POST /v1/projects', :key, :payload_hash, 0, '{}'::jsonb) "
                    "ON CONFLICT (owner_id, route, idempotency_key) WHERE project_id IS NULL DO NOTHING "
                    "RETURNING id"
                ),
                {"owner": owner["owner_id"], "key": key, "payload_hash": payload_hash},
            ).first()
            if inserted is None:
                existing = conn.execute(
                    text(
                        "SELECT payload_sha256, response_status, response_body FROM idempotency_keys "
                        "WHERE owner_id = :owner AND project_id IS NULL AND route = 'POST /v1/projects' AND idempotency_key = :key"
                    ),
                    {"owner": owner["owner_id"], "key": key},
                ).mappings().one()
                if existing["payload_sha256"] != payload_hash or existing["response_status"] == 0:
                    raise ApiError(409, "idempotency_conflict", "同一个幂等键对应了不同内容或尚未完成")
                return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
            try:
                row = conn.execute(
                    text(
                        "INSERT INTO projects (owner_id, name) VALUES (:owner, :name) "
                        "RETURNING id::text AS id, name, version, visibility_epoch, created_at"
                    ),
                    {"owner": owner["owner_id"], "name": name.strip()},
                ).mappings().one()
            except IntegrityError as exc:
                raise ApiError(409, "project_conflict", "项目未能创建") from exc
            project = _project(row)
            conn.execute(
                text(
                    "UPDATE idempotency_keys SET response_status = 201, response_body = CAST(:body AS jsonb) "
                    "WHERE owner_id = :owner AND project_id IS NULL AND route = 'POST /v1/projects' AND idempotency_key = :key"
                ),
                {"owner": owner["owner_id"], "key": key, "body": json.dumps(project)},
            )
        return JSONResponse(status_code=201, content=project)

    @app.post("/v1/projects/{project_id}/artifacts", status_code=201, operation_id="uploadArtifact")
    async def upload_artifact(project_id: str, request: Request, file: UploadFile) -> JSONResponse:
        _require_origin(request, resolved)
        owner = _service(app).require_csrf(_cookie(request), request.headers.get("x-csrf-token"))
        key = request.headers.get("idempotency-key")
        if key is None or not 8 <= len(key) <= 128:
            raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key 长度必须在 8 到 128 之间")
        _require_owned_project(app.state.database, owner["owner_id"], project_id)
        try:
            data = await read_bounded_upload(file, MAX_UPLOAD_BYTES)
        except ArtifactError as exc:
            raise ApiError(413, exc.code, exc.message) from exc
        media_type = (file.content_type or "").split(";")[0].strip()
        payload_hash = sha256_text(f"{media_type}:{hashlib.sha256(data).hexdigest()}")
        route = f"POST /v1/projects/{project_id}/artifacts"
        try:
            with app.state.database.transaction(owner_id=owner["owner_id"], project_id=project_id) as conn:
                inserted = conn.execute(
                    text(
                        "INSERT INTO idempotency_keys (owner_id, project_id, route, idempotency_key, payload_sha256, response_status, response_body) "
                        "VALUES (:owner, :project_id, :route, :key, :payload_hash, 0, '{}'::jsonb) "
                        "ON CONFLICT (owner_id, project_id, route, idempotency_key) WHERE project_id IS NOT NULL DO NOTHING RETURNING id"
                    ),
                    {"owner": owner["owner_id"], "project_id": project_id, "route": route, "key": key, "payload_hash": payload_hash},
                ).first()
                if inserted is None:
                    existing = conn.execute(
                        text("SELECT payload_sha256, response_status, response_body FROM idempotency_keys WHERE owner_id = :owner AND project_id = :project_id AND route = :route AND idempotency_key = :key"),
                        {"owner": owner["owner_id"], "project_id": project_id, "route": route, "key": key},
                    ).mappings().one()
                    if existing["payload_sha256"] != payload_hash or existing["response_status"] != 201:
                        raise ApiError(409, "idempotency_conflict", "同一个幂等键对应了不同内容或尚未完成")
                    return JSONResponse(status_code=existing["response_status"], content=existing["response_body"])
                stored = _artifacts(app, resolved).put(owner_id=owner["owner_id"], project_id=project_id, data=data, media_type=media_type, conn=conn)
                conn.execute(
                    text("UPDATE idempotency_keys SET response_status = 201, response_body = CAST(:body AS jsonb) WHERE owner_id = :owner AND project_id = :project_id AND route = :route AND idempotency_key = :key"),
                    {"owner": owner["owner_id"], "project_id": project_id, "route": route, "key": key, "body": json.dumps(stored)},
                )
        except ArtifactError as exc:
            status = 413 if exc.code == "size_rejected" else 422
            raise ApiError(status, exc.code, exc.message) from exc
        return JSONResponse(status_code=201, content=stored)

    @app.get("/v1/projects/{project_id}/artifacts/{artifact_id}", operation_id="getArtifact")
    def download_artifact(project_id: str, artifact_id: str, request: Request) -> Response:
        owner = _service(app).authenticate(_cookie(request))
        _require_owned_project(app.state.database, owner["owner_id"], project_id)
        try:
            data, media_type = _artifacts(app, resolved).get(owner_id=owner["owner_id"], project_id=project_id, artifact_id=artifact_id)
        except ArtifactError as exc:
            raise ApiError(404, exc.code, exc.message) from exc
        return Response(content=data, media_type=media_type)

    @app.delete("/v1/projects/{project_id}/artifacts/{artifact_id}", status_code=204, operation_id="deleteArtifact")
    def delete_artifact(project_id: str, artifact_id: str, request: Request) -> Response:
        _require_origin(request, resolved)
        owner = _service(app).require_csrf(_cookie(request), request.headers.get("x-csrf-token"))
        _require_owned_project(app.state.database, owner["owner_id"], project_id)
        try:
            _artifacts(app, resolved).tombstone(owner_id=owner["owner_id"], project_id=project_id, artifact_id=artifact_id)
        except ArtifactError as exc:
            status = 409 if exc.code == "evidence_retained" else 404
            raise ApiError(status, exc.code, exc.message) from exc
        return Response(status_code=204)

    register_control_routes(app, resolved)
    register_memory_routes(app, resolved)
    register_console_routes(app, resolved)
    return app


def _artifacts(app: FastAPI, settings: Settings) -> ArtifactStore:
    if app.state.database is None:
        raise ApiError(503, "database_unconfigured", "数据库未配置")
    return ArtifactStore(settings.artifact_root, app.state.database)


def _require_owned_project(database: Database, owner_id: str, project_id: str) -> None:
    try:
        uuid.UUID(project_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "项目不存在") from exc
    with database.transaction(owner_id=owner_id) as conn:
        row = conn.execute(text("SELECT id FROM projects WHERE id = :id AND deleted_at IS NULL"), {"id": project_id}).first()
    if row is None:
        raise ApiError(404, "not_found", "项目不存在")


def _service(app: FastAPI) -> AuthService:
    if app.state.auth is None:
        raise ApiError(503, "database_unconfigured", "数据库未配置")
    return app.state.auth


def _cookie(request: Request) -> str:
    token = request.cookies.get(COOKIE)
    if not token:
        raise ApiError(401, "unauthenticated", "未登录")
    return token


def _client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _require_loopback(request: Request, settings: Settings) -> None:
    host = _client_host(request)
    if host in {"127.0.0.1", "::1"}:
        return
    if settings.allow_testclient and host == "testclient":
        return
    raise ApiError(403, "bootstrap_not_loopback", "引导只能在本机回环地址进行")


def _require_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    if origin not in settings.origins:
        raise ApiError(403, "origin_rejected", "Origin 不在允许列表")


def _require_fields(body: dict[str, Any], *names: str) -> None:
    if not isinstance(body, dict) or any(name not in body for name in names):
        raise ApiError(422, "invalid_body", "请求体缺少字段")


def _session_response(session: dict[str, Any], settings: Settings, *, status: int) -> JSONResponse:
    body = {
        "owner_id": session["owner_id"],
        "display_name": session["display_name"],
        "csrf_token": session["csrf_token"],
        "expires_at": session["expires_at"].isoformat().replace("+00:00", "Z"),
    }
    response = JSONResponse(status_code=status, content=body)
    response.set_cookie(
        COOKIE,
        session["token"],
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.session_hours * 3600,
    )
    return response


def _project(row: Any) -> dict[str, Any]:
    created = row["created_at"]
    return {
        "id": row["id"],
        "name": row["name"],
        "version": int(row["version"]),
        "visibility_epoch": int(row["visibility_epoch"]),
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }


def _encode_cursor(created_at: datetime, project_id: str) -> str:
    raw = f"{created_at.isoformat()}|{project_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if cursor is None:
        return None, None
    try:
        text_value = base64.urlsafe_b64decode(cursor.encode()).decode()
        created, project_id = text_value.split("|", 1)
        return datetime.fromisoformat(created), project_id
    except (ValueError, UnicodeError) as exc:
        raise ApiError(422, "invalid_cursor", "分页游标无效") from exc


def _error(request: Request, code: str, message: str, retryable: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "request_id": getattr(request.state, "request_id", ""),
        "retryable": retryable,
        "details": details,
    }
