"""Explainable project recommendations. This is not collaborative filtering."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from aether_storage.db import Database

POLICY_VERSION = "score-v1"
EXPLORATION_RATE = 0.05


class RecommendationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _profile(conn: Any, project_id: str) -> dict[str, Any]:
    row = conn.execute(
        text(
            "SELECT id::text AS id, profile_version, feature_config FROM profile_snapshots "
            "WHERE project_id = :project_id ORDER BY profile_version DESC LIMIT 1"
        ),
        {"project_id": project_id},
    ).mappings().first()
    if row is None:
        inserted = conn.execute(
            text(
                "INSERT INTO profile_snapshots (project_id, profile_version, feedback_watermark, feature_config) "
                "VALUES (:project_id, 1, 0, CAST(:config AS jsonb)) RETURNING id::text"
            ),
            {"project_id": project_id, "config": json.dumps({"cleared": False, "method": "content_features"})},
        ).scalar_one()
        return {"id": str(inserted), "profile_version": 1, "feature_config": {"cleared": False, "method": "content_features"}}
    config = row["feature_config"]
    if isinstance(config, str):
        config = json.loads(config)
    return {"id": row["id"], "profile_version": int(row["profile_version"]), "feature_config": config}


def rank_candidates(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    candidates: list[dict[str, Any]],
    explore: bool = False,
) -> dict[str, Any]:
    """Rank visible candidates. Exploration stays off unless the caller turns it on."""
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        hidden = {
            str(item)
            for item in conn.execute(
                text(
                    "SELECT source_id::text FROM feedback_events "
                    "WHERE project_id = :project_id AND kind = 'hide' AND deleted_at IS NULL"
                ),
                {"project_id": project_id},
            ).scalars()
        }
        visible = [item for item in candidates if not item.get("deleted") and str(item["source_id"]) not in hidden]
        ordered = sorted(visible, key=lambda item: (-float(item["goal_match"]), str(item["source_id"])))
        reasons = [
            {"source_id": str(item["source_id"]), "reason": "当前目标匹配", "goal_match": float(item["goal_match"])}
            for item in ordered
        ]
        probabilities: dict[str, float] = {}
        if explore and ordered:
            count = len(ordered)
            best = str(ordered[0]["source_id"])
            for item in ordered:
                source_id = str(item["source_id"])
                probabilities[source_id] = (1 - EXPLORATION_RATE) + (EXPLORATION_RATE / count) if source_id == best else EXPLORATION_RATE / count
        profile = _profile(conn, project_id)
        impression_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO recommendation_impressions "
                "(id, project_id, profile_id, policy_version, candidates, displayed_items, propensity_details) "
                "VALUES (:id, :project_id, :profile_id, :policy, CAST(:candidates AS jsonb), CAST(:displayed AS jsonb), CAST(:propensity AS jsonb))"
            ),
            {
                "id": impression_id,
                "project_id": project_id,
                "profile_id": profile["id"],
                "policy": POLICY_VERSION,
                "candidates": json.dumps(reasons, ensure_ascii=False),
                "displayed": json.dumps([item["source_id"] for item in ordered]),
                "propensity": json.dumps({"explore": explore, "probabilities": probabilities, "collaborative_filtering": False}),
            },
        )
    return {
        "impression_id": impression_id,
        "policy_version": POLICY_VERSION,
        "explore": explore,
        "items": reasons,
        "probabilities": probabilities,
        "collaborative_filtering": False,
    }


def record_feedback(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    impression_id: str,
    source_id: str,
    kind: str,
    event_key: str,
) -> dict[str, Any]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        feedback_id = conn.execute(
            text(
                "INSERT INTO feedback_events (project_id, event_key, impression_id, source_id, kind, event_time) "
                "VALUES (:project_id, :event_key, :impression_id, :source_id, :kind, now()) "
                "ON CONFLICT (project_id, event_key) DO NOTHING RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "event_key": event_key,
                "impression_id": impression_id,
                "source_id": source_id,
                "kind": kind,
            },
        ).scalar()
        if feedback_id is None:
            return {"recorded": False, "reason": "duplicate", "feedback_id": None}
    return {"recorded": True, "reason": "stored", "feedback_id": str(feedback_id)}


def clear_profile(database: Database, *, owner_id: str, project_id: str) -> dict[str, Any]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        version = conn.execute(
            text("SELECT COALESCE(MAX(profile_version), 0) + 1 FROM profile_snapshots WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one()
        profile_id = conn.execute(
            text(
                "INSERT INTO profile_snapshots (project_id, profile_version, feedback_watermark, feature_config) "
                "VALUES (:project_id, :version, 0, CAST(:config AS jsonb)) RETURNING id::text"
            ),
            {"project_id": project_id, "version": int(version), "config": json.dumps({"cleared": True, "method": "content_features"})},
        ).scalar_one()
    return {"profile_id": str(profile_id), "rebuildable": True, "collaborative_filtering": False}
