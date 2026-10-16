"""Finish work the console has already queued. A queued row is not a terminal state."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import httpcore
import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.db import Database
from aether_worker.document_text import document_text
from aether_worker.html_text import page_text
from aether_worker.ingest import TEXT_MEDIA, IngestError, attach_text_version, fetch_url, parse_rss_items
from aether_worker.remote_sources import arxiv_text, fetch_arxiv, fetch_github, github_text
from aether_worker.retrieval import RetrievalError, activate_generation, build_generation
from aether_worker.watches import trigger_watch

_SUPPORTED_KINDS = {"local_document", "url", "rss", "github_api", "arxiv_api"}
_FETCH_FAILURES = (httpx.HTTPError, httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException)


def execute_queued_ingestions(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    index_root: Path,
) -> None:
    for job in _claim_ingestions(database, owner_id=owner_id, project_id=project_id):
        _run_ingestion(database, store, owner_id=owner_id, project_id=project_id, index_root=index_root, job=job)


def finish_watch_tick(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    watch_id: str,
    trigger_key: str,
) -> None:
    _run_watch(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        watch_id=watch_id,
        trigger_key=trigger_key,
    )


def _claim_ingestions(database: Database, *, owner_id: str, project_id: str) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT id::text AS id, source_id::text AS source_id, source_version_id::text AS source_version_id "
                "FROM ingestion_jobs WHERE project_id = :project_id AND status IN ('QUEUED', 'RUNNING') "
                "ORDER BY created_at, id FOR UPDATE"
            ),
            {"project_id": project_id},
        ).mappings().all()
        claimed = [dict(row) for row in rows]
        if not claimed:
            return []
        conn.execute(
            text("UPDATE ingestion_jobs SET status = 'RUNNING' WHERE project_id = :project_id AND id::text = ANY(:ids)"),
            {"project_id": project_id, "ids": [row["id"] for row in claimed]},
        )
    return claimed


def _run_ingestion(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    index_root: Path,
    job: dict[str, Any],
) -> None:
    version_id = None if job["source_version_id"] is None else str(job["source_version_id"])
    try:
        if version_id is None:
            version_id = _store_source(database, store, owner_id=owner_id, project_id=project_id, source_id=str(job["source_id"]))
            _remember_version(database, owner_id=owner_id, project_id=project_id, job_id=str(job["id"]), version_id=version_id)
        print(f"aether-ingestion-version job={job['id']} version={version_id}", flush=True)
        _publish_index(database, store, owner_id=owner_id, project_id=project_id, index_root=index_root, job_id=str(job["id"]))
    except (IngestError, RetrievalError, SQLAlchemyError, OSError, UnicodeError, json.JSONDecodeError, *_FETCH_FAILURES) as exc:
        print(f"aether-ingestion-failed job={job['id']} error={_code(exc)}", flush=True)
        _finish(database, owner_id=owner_id, project_id=project_id, job_id=str(job["id"]), status="FAILED", error_code=_code(exc), version_id=version_id)
        return
    _finish(database, owner_id=owner_id, project_id=project_id, job_id=str(job["id"]), status="SUCCEEDED", error_code=None, version_id=version_id)


def _store_source(database: Database, store: ArtifactStore, *, owner_id: str, project_id: str, source_id: str) -> str:
    source = _load_source(database, owner_id=owner_id, project_id=project_id, source_id=source_id)
    raw, text_value, raw_media_type = _read_body(source, store=store, owner_id=owner_id, project_id=project_id)
    stored = attach_text_version(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        source_id=source_id,
        raw=raw,
        text_value=text_value,
        raw_media_type=raw_media_type,
    )
    return stored["source_version_id"]


def _load_source(database: Database, *, owner_id: str, project_id: str, source_id: str) -> dict[str, Any]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT kind, locator, status, approved_config FROM sources "
                "WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"
            ),
            {"project_id": project_id, "id": source_id},
        ).mappings().first()
    if row is None:
        raise IngestError("source_missing")
    if str(row["status"]) == "PAUSED":
        raise IngestError("source_paused")
    return dict(row)


def _read_body(
    source: dict[str, Any],
    *,
    store: ArtifactStore,
    owner_id: str,
    project_id: str,
) -> tuple[bytes, str, str]:
    kind = str(source["kind"])
    locator = str(source["locator"])
    if kind not in _SUPPORTED_KINDS:
        raise IngestError("unsupported")
    if kind == "local_document":
        raw, media_type = _artifact_bytes(store, owner_id=owner_id, project_id=project_id, locator=locator)
        return raw, document_text(raw, media_type), media_type
    domains = _allowed_domains(source)
    if kind == "url":
        raw = fetch_url(locator, allowed_domains=domains)
        return raw, page_text(raw), TEXT_MEDIA
    if kind == "rss":
        raw, text_value = _decoded(_remote_bytes(kind, locator, domains))
        return raw, text_value, TEXT_MEDIA
    if kind == "github_api":
        raw = fetch_github(locator, allowed_domains=domains)
        return raw, github_text(raw), TEXT_MEDIA
    if kind == "arxiv_api":
        raw = fetch_arxiv(locator, allowed_domains=domains)
        return raw, arxiv_text(raw), TEXT_MEDIA
    raise IngestError("unsupported")


def _decoded(raw: bytes) -> tuple[bytes, str]:
    try:
        return raw, raw.decode("utf-8")
    except UnicodeError as exc:
        raise IngestError("parse_rejected") from exc


def _allowed_domains(source: dict[str, Any]) -> set[str]:
    config = source.get("approved_config")
    if isinstance(config, str):
        config = json.loads(config)
    if not isinstance(config, dict):
        return set()
    domains = config.get("allowed_domains")
    if not isinstance(domains, list):
        return set()
    return {str(item) for item in domains if isinstance(item, str)}


def _artifact_bytes(store: ArtifactStore, *, owner_id: str, project_id: str, locator: str) -> tuple[bytes, str]:
    try:
        artifact_id = str(uuid.UUID(locator.strip()))
    except ValueError as exc:
        raise IngestError("artifact_missing") from exc
    try:
        data, media_type = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
    except ArtifactError as exc:
        raise IngestError(exc.code) from exc
    return data, str(media_type)


def _remote_bytes(kind: str, locator: str, domains: set[str]) -> bytes:
    body = fetch_url(locator, allowed_domains=domains)
    if kind == "url":
        return body
    if kind == "rss":
        return "\n".join(parse_rss_items(body)).encode()
    raise IngestError("unsupported")


def _remember_version(database: Database, *, owner_id: str, project_id: str, job_id: str, version_id: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET source_version_id = :version_id "
                "WHERE project_id = :project_id AND id = :id AND status = 'RUNNING'"
            ),
            {"version_id": version_id, "project_id": project_id, "id": job_id},
        )


def _publish_index(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    index_root: Path,
    job_id: str,
) -> None:
    key = "g" + job_id.replace("-", "")
    try:
        generation_id = build_generation(
            database,
            store,
            owner_id=owner_id,
            project_id=project_id,
            generation_key=key,
            index_root=index_root,
            force=True,
        )
    except RetrievalError as exc:
        if exc.code != "generation_key_rejected":
            raise
        generation_id = _generation_id(database, owner_id=owner_id, project_id=project_id, generation_key=key)
        if generation_id is None:
            raise
    _activate(database, owner_id=owner_id, project_id=project_id, generation_id=generation_id)


def _generation_id(database: Database, *, owner_id: str, project_id: str, generation_key: str) -> str | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT id::text FROM index_generations WHERE project_id = :project_id AND generation_key = :key"),
            {"project_id": project_id, "key": generation_key},
        ).scalar()
    return None if found is None else str(found)


def _activate(database: Database, *, owner_id: str, project_id: str, generation_id: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        state = conn.execute(
            text("SELECT state FROM index_generations WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": generation_id},
        ).scalar()
    if state == "ACTIVE":
        return
    if state != "READY":
        raise RetrievalError("activation_rejected")
    activate_generation(database, owner_id=owner_id, project_id=project_id, generation_id=generation_id)


def _finish(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    job_id: str,
    status: str,
    error_code: str | None,
    version_id: str | None,
) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET status = :status, error_code = :error_code, source_version_id = :version_id, finished_at = now() "
                "WHERE project_id = :project_id AND id = :id AND status = 'RUNNING'"
            ),
            {
                "status": status,
                "error_code": error_code,
                "version_id": version_id,
                "project_id": project_id,
                "id": job_id,
            },
        )


def _run_watch(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    watch_id: str,
    trigger_key: str,
) -> None:
    result = trigger_watch(
        database,
        owner_id=owner_id,
        project_id=project_id,
        watch_id=watch_id,
        watermark=_watermark(database, owner_id=owner_id, project_id=project_id, watch_id=watch_id),
        trigger_key=trigger_key,
    )
    if not result["summarized"]:
        return
    store.put(
        owner_id=owner_id,
        project_id=project_id,
        data=f"watch-run\n{watch_id}\n".encode(),
        media_type="text/plain",
    )


def _watermark(database: Database, *, owner_id: str, project_id: str, watch_id: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        raw = conn.execute(
            text("SELECT source_ids FROM watches WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": watch_id},
        ).scalar_one()
        sources = raw if isinstance(raw, list) else json.loads(raw)
        rows = conn.execute(
            text(
                "SELECT locator FROM sources WHERE project_id = :project_id AND id::text = ANY(:ids) AND deleted_at IS NULL ORDER BY id"
            ),
            {"project_id": project_id, "ids": [str(item) for item in sources]},
        ).scalars().all()
    if not rows:
        return "empty"
    return hashlib.sha256("\n".join(str(item) for item in rows).encode()).hexdigest()


def _code(exc: Exception) -> str:
    if isinstance(exc, _FETCH_FAILURES):
        return "fetch_failed"
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(exc).__name__
