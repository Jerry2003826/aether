"""Independent skill evaluation. The actor cannot write the score."""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database

THRESHOLDS = {"min_families": 30, "min_success_delta": 0.05, "min_ci_lower": 0.0, "max_cost_ratio": 1.10}
REPORT_CLASSIFICATION = "skill_evaluation_report"


class SkillEvalError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _insufficient() -> dict[str, Any]:
    return {"decision": "INSUFFICIENT_EVIDENCE", "label": "not_synthetic", "promoted": False, "learned": False}


def _rejected() -> dict[str, Any]:
    return {"decision": "REJECTED", "label": "not_synthetic", "promoted": False, "learned": False}


def _families(value: Any) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _finite_in(value: Any, low: float, high: float | None) -> float | None:
    """Real int/float inside the metric's closed range. Bool, str, NaN and inf are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < low:
        return None
    if high is not None and number > high:
        return None
    return number


def _measured(metrics: dict[str, Any]) -> tuple[int, float, float, float] | None:
    if not isinstance(metrics, dict):
        return None
    if "families" not in metrics or "ci_lower" not in metrics or "success_delta" not in metrics or "cost_ratio" not in metrics:
        return None
    families = _families(metrics["families"])
    ci_lower = _finite_in(metrics["ci_lower"], -1.0, 1.0)
    delta = _finite_in(metrics["success_delta"], -1.0, 1.0)
    cost_ratio = _finite_in(metrics["cost_ratio"], 0.0, None)
    if families is None or ci_lower is None or delta is None or cost_ratio is None:
        return None
    return families, ci_lower, delta, cost_ratio


def judge(metrics: dict[str, Any], *, synthetic: bool) -> dict[str, Any]:
    if synthetic:
        return {"decision": "REJECTED", "label": "synthetic_control", "promoted": False, "learned": False}
    measured = _measured(metrics)
    if measured is None:
        return _insufficient()
    families, ci_lower, delta, cost_ratio = measured
    if metrics.get("security_failure"):
        return _rejected()
    if families < THRESHOLDS["min_families"] or ci_lower <= THRESHOLDS["min_ci_lower"]:
        return _insufficient()
    if delta < THRESHOLDS["min_success_delta"] or cost_ratio > THRESHOLDS["max_cost_ratio"]:
        return _rejected()
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
    report_store: ArtifactStore | None = None,
    evaluation_id: str | None = None,
    baseline_version_id: str | None = None,
) -> str:
    """Record a verifier evaluation. With a report store, the report is written in the same transaction.

    Only the verifier can insert evaluation_runs and nobody can update them, so the report hash kept in
    ``configuration`` is the reference a promotion checks the artifact ledger and the bytes against.
    """
    if split == "holdout" and purpose != "release":
        raise SkillEvalError("dataset_isolated")
    verdict = judge(metrics, synthetic=synthetic)
    evaluation_id = evaluation_id or str(uuid.uuid4())
    recorded = {**metrics, "synthetic": synthetic, "label": verdict["label"]}
    configuration: dict[str, Any] = {"split": split, "label": verdict["label"]}
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        identity = str(conn.execute(text("SELECT current_user")).scalar_one())
        if identity != "aether_verifier":
            raise SkillEvalError("grader_required")
        report_artifact_id = None
        if report_store is not None:
            report = evaluation_report(
                evaluation_id=evaluation_id,
                candidate_version_id=candidate_version_id,
                dataset_sha256=dataset_sha256,
                purpose=purpose,
                split=split,
                status=verdict["decision"],
                metrics=recorded,
                evaluator_identity=identity,
            )
            stored = report_store.put(
                owner_id=owner_id, project_id=project_id, data=report, media_type="application/json",
                classification=REPORT_CLASSIFICATION, conn=conn,
            )
            report_artifact_id = str(stored["artifact_id"])
            configuration["report_sha256"] = str(stored["sha256"])
        conn.execute(
            text(
                "INSERT INTO evaluation_runs (id, project_id, purpose, dataset_manifest_sha256, model_manifest, "
                "candidate_version_id, baseline_version_id, configuration, status, metrics, report_artifact_id, evaluator_identity) "
                "VALUES (:id, :project_id, :purpose, :dataset, CAST(:model AS jsonb), :candidate, :baseline, CAST(:configuration AS jsonb), "
                ":status, CAST(:metrics AS jsonb), :report, :identity)"
            ),
            {
                "id": evaluation_id,
                "project_id": project_id,
                "purpose": purpose,
                "dataset": dataset_sha256,
                "model": json.dumps({"scripted": True}),
                "candidate": candidate_version_id,
                "baseline": baseline_version_id,
                "configuration": json.dumps(configuration),
                "status": verdict["decision"],
                "metrics": json.dumps(recorded),
                "report": report_artifact_id,
                "identity": identity,
            },
        )
    return evaluation_id


def evaluation_report(
    *,
    evaluation_id: str,
    candidate_version_id: str,
    dataset_sha256: str,
    purpose: str,
    split: str,
    status: str,
    metrics: dict[str, Any],
    evaluator_identity: str,
) -> bytes:
    report = {
        "kind": REPORT_CLASSIFICATION,
        "evaluation_id": evaluation_id,
        "candidate_version_id": candidate_version_id,
        "dataset_manifest_sha256": dataset_sha256,
        "purpose": purpose,
        "split": split,
        "status": status,
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "evaluator_identity": evaluator_identity,
    }
    return json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def apply_verdict(database: Database, *, owner_id: str, project_id: str, version_id: str, verdict: dict[str, Any]) -> str:
    status = "ELIGIBLE" if verdict["promoted"] else "REJECTED"
    if verdict["decision"] == "INSUFFICIENT_EVIDENCE":
        status = "EVALUATING"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        updated = conn.execute(
            text(
                "UPDATE skill_versions SET status = :status, "
                "manifest = jsonb_set(manifest, '{status}', to_jsonb(CAST(:status AS text)), true) "
                "WHERE project_id = :project_id AND id = :id AND status NOT IN ('CANARY', 'ACTIVE')"
            ),
            {"status": status, "project_id": project_id, "id": version_id},
        ).rowcount
        if updated == 1:
            return status
        current = conn.execute(
            text("SELECT status FROM skill_versions WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": version_id},
        ).scalar()
    return status if current is None else str(current)


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
