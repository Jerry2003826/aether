"""Tombstones and derived-index invalidation. A tombstone is not a physical delete."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

LIMITS = (
    "这是 tombstone，不是从磁盘和备份里物理删除。",
    "数据库备份和 Temporal 历史里可能还留着副本，要等备份到期才消失。",
)
_SEARCH_CACHE: dict[tuple[str, int, str, str], dict[str, Any]] = {}


class RetentionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def current_epoch(conn: Any, project_id: str) -> int:
    value = conn.execute(
        text("SELECT visibility_epoch FROM projects WHERE id = :id"),
        {"id": project_id},
    ).scalar_one()
    return int(value)


def cache_result(project_id: str, epoch: int, key: str, value: dict[str, Any], *, generation_id: str = "") -> None:
    _SEARCH_CACHE[(project_id, int(epoch), generation_id, key)] = dict(value)


def read_cached_result(conn: Any, project_id: str, key: str, *, generation_id: str = "") -> dict[str, Any] | None:
    """Return a cached search only for the current epoch and the requested generation."""
    found = _SEARCH_CACHE.get((project_id, current_epoch(conn, project_id), generation_id, key))
    return None if found is None else dict(found)


def _drop_project_cache(project_id: str) -> None:
    for cache_key in list(_SEARCH_CACHE):
        if cache_key[0] == project_id:
            del _SEARCH_CACHE[cache_key]


def invalidate_derived(conn: Any, *, project_id: str) -> dict[str, Any]:
    """Bump the visibility epoch and drop derived indexes that could serve deleted text."""
    previous = current_epoch(conn, project_id)
    _drop_project_cache(project_id)
    retired = conn.execute(
        text(
            "UPDATE index_generations SET state = 'RETIRED' "
            "WHERE project_id = :project_id AND state IN ('BUILDING', 'READY', 'ACTIVE') "
            "RETURNING generation_key"
        ),
        {"project_id": project_id},
    ).scalars().all()
    cleared = conn.execute(
        text(
            "UPDATE chunks SET embedding = NULL, embedding_revision = NULL "
            "WHERE project_id = :project_id AND deleted_at IS NOT NULL AND embedding IS NOT NULL"
        ),
        {"project_id": project_id},
    ).rowcount
    version = conn.execute(
        text("SELECT COALESCE(MAX(profile_version), 0) + 1 FROM profile_snapshots WHERE project_id = :project_id"),
        {"project_id": project_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO profile_snapshots (project_id, profile_version, feedback_watermark, feature_config) "
            "VALUES (:project_id, :version, 0, CAST(:config AS jsonb))"
        ),
        {
            "project_id": project_id,
            "version": int(version),
            "config": json.dumps({"invalidated": True, "reason": "visibility_epoch"}, sort_keys=True),
        },
    )
    epoch = conn.execute(
        text("UPDATE projects SET visibility_epoch = visibility_epoch + 1 WHERE id = :id RETURNING visibility_epoch"),
        {"id": project_id},
    ).scalar_one()
    return {
        "previous_epoch": previous,
        "visibility_epoch": int(epoch),
        "retired_generations": [str(item) for item in retired],
        "embeddings_cleared": int(cleared or 0),
        "physical_delete": False,
        "limits": list(LIMITS),
    }


def commit_embedding(conn: Any, *, project_id: str, chunk_id: str, embedding: str, revision: str, seen_epoch: int) -> bool:
    """Write a vector only when the chunk is still visible at the epoch the job observed."""
    if current_epoch(conn, project_id) != int(seen_epoch):
        return False
    updated = conn.execute(
        text(
            "UPDATE chunks SET embedding = CAST(:embedding AS vector), embedding_revision = :revision "
            "WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"
        ),
        {"embedding": embedding, "revision": revision, "project_id": project_id, "id": chunk_id},
    ).rowcount
    return updated == 1


def commit_claim_text(conn: Any, *, project_id: str, memory_id: str, claim: str, seen_epoch: int) -> bool:
    """A late summary may not put text back onto a forgotten memory."""
    if current_epoch(conn, project_id) != int(seen_epoch):
        return False
    updated = conn.execute(
        text(
            "UPDATE memories SET claim = :claim "
            "WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"
        ),
        {"claim": claim, "project_id": project_id, "id": memory_id},
    ).rowcount
    return updated == 1
