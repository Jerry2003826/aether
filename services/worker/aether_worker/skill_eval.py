"""Independent skill evaluation. The actor cannot write the score."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from aether_storage.db import Database

THRESHOLDS = {"min_families": 30, "min_success_delta": 0.05, "min_ci_lower": 0.0, "max_cost_ratio": 1.10}


class SkillEvalError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def judge(metrics: dict[str, Any], *, synthetic: bool) -> dict[str, Any]:
    if synthetic:
        return {"decision": "REJECTED", "label": "synthetic_control", "promoted": False, "learned": False}
    families = int(metrics.get("families") or 0)
    ci_lower = float(metrics.get("ci_lower") or 0)
    delta = float(metrics.get("success_delta") or 0)
    cost_ratio = float(metrics.get("cost_ratio") or 99)
    if families < THRESHOLDS["min_families"] or ci_lower <= THRESHOLDS["min_ci_lower"]:
        return {"decision": "INSUFFICIENT_EVIDENCE", "label": "not_synthetic", "promoted": False, "learned": False}
    if delta < THRESHOLDS["min_success_delta"] or cost_ratio > THRESHOLDS["max_cost_ratio"] or metrics.get("security_failure"):
        return {"decision": "REJECTED", "label": "not_synthetic", "promoted": False, "learned": False}
    return {"decision": "ELIGIBLE", "label": "not_synthetic", "promoted": True, "learned": False}


def record_evaluation(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    candidate_version_id: str,
    dataset_sha256: str,
    purpose: str,
    split: str,
    metrics: dict[str, Any],
    synthetic: bool,
) -> str:
    if split == "holdout" and purpose != "release":
        raise SkillEvalError("dataset_isolated")
    verdict = judge(metrics, synthetic=synthetic)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        identity = str(conn.execute(text("SELECT current_user")).scalar_one())
        if identity != "aether_verifier":
            raise SkillEvalError("grader_required")
        row = conn.execute(
            text(
                "INSERT INTO evaluation_runs (project_id, purpose, dataset_manifest_sha256, model_manifest, "
                "candidate_version_id, configuration, status, metrics, evaluator_identity) "
                "VALUES (:project_id, :purpose, :dataset, CAST(:model AS jsonb), :candidate, CAST(:configuration AS jsonb), "
                ":status, CAST(:metrics AS jsonb), :identity) RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "purpose": purpose,
                "dataset": dataset_sha256,
                "model": json.dumps({"scripted": True}),
                "candidate": candidate_version_id,
                "configuration": json.dumps({"split": split, "label": verdict["label"]}),
                "status": verdict["decision"],
                "metrics": json.dumps({**metrics, "synthetic": synthetic, "label": verdict["label"]}),
                "identity": identity,
            },
        ).scalar_one()
    return str(row)


def apply_verdict(database: Database, *, owner_id: str, project_id: str, version_id: str, verdict: dict[str, Any]) -> str:
    status = "ELIGIBLE" if verdict["promoted"] else "REJECTED"
    if verdict["decision"] == "INSUFFICIENT_EVIDENCE":
        status = "EVALUATING"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE skill_versions SET status = :status WHERE project_id = :project_id AND id = :id"),
            {"status": status, "project_id": project_id, "id": version_id},
        )
    return status


def approve_canary(
    database: Database,
    *,
    owner_id: str,
    actor_id: str,
    project_id: str,
    skill_id: str,
    version_id: str,
    evaluation_id: str,
    approval_id: str,
    task_id: str,
) -> dict[str, Any]:
    if actor_id != owner_id:
        raise SkillEvalError("owner_required")
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE skill_versions SET status = 'CANARY' WHERE project_id = :project_id AND id = :id AND status = 'ELIGIBLE'"),
            {"project_id": project_id, "id": version_id},
        )
        conn.execute(
            text(
                "INSERT INTO skill_deployments (project_id, skill_id, slot, version_id, evaluation_id, approval_id, deployment_version, traffic_fraction) "
                "VALUES (:project_id, :skill_id, 'canary', :version_id, :evaluation_id, :approval_id, 1, 0.05)"
            ),
            {
                "project_id": project_id,
                "skill_id": skill_id,
                "version_id": version_id,
                "evaluation_id": evaluation_id,
                "approval_id": approval_id,
            },
        )
        conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "SELECT :project_id, :task_id, COALESCE(MAX(contract_version), 0) + 1, CAST(:pin AS jsonb), :sha, :owner, now() "
                "FROM goal_contracts WHERE project_id = :project_id AND task_id = :task_id"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "pin": json.dumps({"skill_version_id": version_id}),
                "sha": "b" * 64,
                "owner": owner_id,
            },
        )
    return {"status": "CANARY", "pinned_version_id": version_id, "traffic_fraction": 0.05}


def rollback_active(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    skill_id: str,
    previous_version_id: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        updated = conn.execute(
            text(
                "UPDATE skill_deployments SET version_id = :version_id, deployment_version = deployment_version + 1 "
                "WHERE project_id = :project_id AND skill_id = :skill_id AND slot = 'canary'"
            ),
            {"version_id": previous_version_id, "project_id": project_id, "skill_id": skill_id},
        ).rowcount
        if updated != 1:
            raise SkillEvalError("rollback_rejected")
        rolled = conn.execute(
            text("UPDATE skill_versions SET status = 'ROLLED_BACK' WHERE project_id = :project_id AND skill_id = :skill_id AND status = 'CANARY'"),
            {"project_id": project_id, "skill_id": skill_id},
        ).rowcount
        if rolled < 1:
            raise SkillEvalError("rollback_rejected")
        pinned = None
        if task_id is not None:
            pinned = conn.execute(
                text(
                    "SELECT content->>'skill_version_id' FROM goal_contracts "
                    "WHERE project_id = :project_id AND task_id = :task_id AND content ? 'skill_version_id' "
                    "ORDER BY contract_version DESC LIMIT 1"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).scalar()
    return {"deployment_version_id": previous_version_id, "pinned_version_id": None if pinned is None else str(pinned), "atomic": updated == 1}
