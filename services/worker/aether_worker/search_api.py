"""Bind one authorised retrieval call to the caller's project and the shared index."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.retrieval import search

_CHANNELS = {"lexical": "bm25", "dense": "dense", "hybrid": "rerank"}


def authorised_search(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    query: str,
    mode: str,
    limit: int,
    source_ids: list[str],
    index_root: Path,
    generation_id: str,
) -> dict[str, Any]:
    found = search(database, owner_id=owner_id, project_id=project_id, query=query, index_root=index_root, generation_id=generation_id)
    hits = _hits(database, owner_id=owner_id, project_id=project_id, found=found, mode=mode, limit=limit, source_ids=source_ids)
    degraded = bool(found["degraded"])
    return {
        "hits": hits,
        "index_generation": generation_id,
        "embedding_revision": found["embedding_revision"],
        "degraded": degraded,
        "degraded_reason": "reranker_unavailable" if degraded else None,
    }


def _hits(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    found: dict[str, Any],
    mode: str,
    limit: int,
    source_ids: list[str],
) -> list[dict[str, Any]]:
    ordered = [str(item) for item in found[_CHANNELS[mode]]]
    if not ordered:
        return []
    allowed = set(source_ids)
    rows = _passages(database, owner_id=owner_id, project_id=project_id, chunk_ids=ordered)
    hits: list[dict[str, Any]] = []
    for chunk_id in ordered:
        row = rows.get(chunk_id)
        if row is None or (allowed and row["source_id"] not in allowed):
            continue
        quote = str(row["text_content"])
        if not quote:
            continue
        start = int(row["start_offset"])
        end = int(row["end_offset"])
        if end <= start:
            continue
        hits.append(
            {
                "chunk_id": chunk_id,
                "document_id": row["source_version_id"],
                "text": quote,
                "score": 1.0 / (len(hits) + 1),
                "citation": {
                    "source_version_id": row["source_version_id"],
                    "source_sha256": row["content_sha256"],
                    "start": start,
                    "end": end,
                    "quote": quote,
                },
            }
        )
        if len(hits) >= limit:
            break
    return hits


def _passages(database: Database, *, owner_id: str, project_id: str, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT c.id::text AS id, c.text_content, c.source_version_id::text AS source_version_id, "
                "c.start_offset, c.end_offset, v.content_sha256, s.id::text AS source_id "
                "FROM chunks c "
                "JOIN source_versions v ON v.project_id = c.project_id AND v.id = c.source_version_id "
                "JOIN sources s ON s.project_id = v.project_id AND s.id = v.source_id "
                "WHERE c.project_id = :project_id AND c.id::text = ANY(:ids) "
                "AND c.deleted_at IS NULL AND s.deleted_at IS NULL AND s.status <> 'revoked'"
            ),
            {"project_id": project_id, "ids": chunk_ids},
        ).mappings().all()
    return {str(row["id"]): dict(row) for row in rows}
