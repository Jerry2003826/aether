"""Grade a queued skill.evaluate command. The score is inserted once and never updated."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.db import Database
from aether_worker.bundle_files import BundleError
from aether_worker.skill_dataset import DatasetError, parse_candidate, parse_dataset
from aether_worker.skill_eval import SkillEvalError, apply_verdict, judge, record_evaluation
from aether_worker.skill_metrics import assert_finite_costs, metrics_from_grades
from aether_worker.verification import VerificationError, export_candidate, grade, text_sha256


def evaluation_deadline(budget: object, *, now: float) -> float | None:
    """None means the request did not set active_seconds. Zero is already due."""
    if not isinstance(budget, dict) or "active_seconds" not in budget:
        return None
    seconds = budget["active_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        raise SkillEvalError("deadline_exceeded")
    return now + float(seconds)


def require_development_split(split: str) -> None:
    """A development evaluation cannot score a holdout manifest."""
    if split == "holdout":
        raise SkillEvalError("dataset_isolated")


def execute_pending_skill_evaluations(
    verifier: Database,
    worker: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    apply_verdicts: bool = True,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for command in _pending(verifier, owner_id=owner_id, project_id=project_id):
        outcomes.append(
            _run_one(
                verifier,
                worker,
                store,
                owner_id=owner_id,
                project_id=project_id,
                command=command,
                apply_verdicts=apply_verdicts,
            )
        )
    return outcomes


def _pending(verifier: Database, *, owner_id: str, project_id: str) -> list[dict[str, Any]]:
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        identity = str(conn.execute(text("SELECT current_user")).scalar_one())
        if identity != "aether_verifier":
            raise SkillEvalError("grader_required")
        rows = conn.execute(
            text(
                "SELECT id::text AS id, command_key, payload FROM command_outbox "
                "WHERE project_id = :project_id AND kind = 'skill.evaluate' AND status = 'PENDING' "
                "ORDER BY created_at, command_key FOR UPDATE"
            ),
            {"project_id": project_id},
        ).mappings().all()
    commands = []
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        commands.append({"id": str(row["id"]), "command_key": str(row["command_key"]), "payload": payload})
    return commands


def _run_one(
    verifier: Database,
    worker: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    command: dict[str, Any],
    apply_verdicts: bool,
) -> dict[str, Any]:
    payload = command["payload"]
    evaluation_id = str(payload["evaluation_id"])
    version_id = str(payload.get("candidate_version_id") or "")
    if _already_recorded(verifier, owner_id=owner_id, project_id=project_id, evaluation_id=evaluation_id):
        verdict = _stored_verdict(verifier, owner_id=owner_id, project_id=project_id, payload=payload)
        if apply_verdicts and verdict is not None and version_id:
            apply_verdict(worker, owner_id=owner_id, project_id=project_id, version_id=version_id, verdict=verdict)
        _finish_command(verifier, owner_id=owner_id, project_id=project_id, command_id=command["id"], status="DELIVERED")
        recorded: dict[str, Any] = {"evaluation_id": evaluation_id, "status": "recorded", "version_id": version_id}
        if verdict is not None:
            recorded["verdict"] = verdict
        return recorded
    try:
        outcome = _grade_payload(store, owner_id=owner_id, project_id=project_id, payload=payload)
        verdict = _record(
            verifier,
            store,
            owner_id=owner_id,
            project_id=project_id,
            payload=payload,
            outcome=outcome,
        )
        if apply_verdicts:
            apply_verdict(
                worker,
                owner_id=owner_id,
                project_id=project_id,
                version_id=version_id,
                verdict=verdict,
            )
        _finish_command(verifier, owner_id=owner_id, project_id=project_id, command_id=command["id"], status="DELIVERED")
        return {"evaluation_id": evaluation_id, "status": str(verdict["decision"]), "version_id": version_id, "verdict": verdict}
    except (SkillEvalError, DatasetError, BundleError, ArtifactError, VerificationError, OSError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, (SkillEvalError, DatasetError, BundleError, ArtifactError, VerificationError)) else "evaluation_failed"
        _record_blocked(
            verifier,
            store,
            owner_id=owner_id,
            project_id=project_id,
            payload=payload,
            code=code,
        )
        _finish_command(verifier, owner_id=owner_id, project_id=project_id, command_id=command["id"], status="DELIVERED")
        return {"evaluation_id": evaluation_id, "status": "REJECTED", "error_code": code, "version_id": version_id}


def _grade_payload(store: ArtifactStore, *, owner_id: str, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    baseline_id = _bound_baseline(store, owner_id=owner_id, project_id=project_id, payload=payload)
    dataset_bytes, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(payload["dataset_manifest_id"]))
    dataset = parse_dataset(dataset_bytes)
    require_development_split(dataset["split"])
    assert_finite_costs(int(dataset["candidate_cost"]), int(dataset["baseline_cost"]))
    candidate_bytes = _candidate_bytes(store, owner_id=owner_id, project_id=project_id, version_id=str(payload["candidate_version_id"]))
    candidate = parse_candidate(candidate_bytes)
    deadline = evaluation_deadline(payload.get("budget"), now=time.monotonic())
    grades = []
    for case in dataset["cases"]:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise SkillEvalError("deadline_exceeded")
        grades.append(_grade_case(candidate["files"], case["suite_files"], dataset["contract"], timeout_seconds=remaining))
    metrics = metrics_from_grades(
        grades,
        baseline_passes=int(dataset["baseline_passes"]),
        candidate_cost=int(dataset["candidate_cost"]),
        baseline_cost=int(dataset["baseline_cost"]),
    )
    return {
        "dataset_sha256": _sha(dataset_bytes),
        "split": dataset["split"],
        "synthetic": dataset["synthetic"],
        "metrics": metrics,
        "baseline_version_id": baseline_id,
    }


def _bound_baseline(store: ArtifactStore, *, owner_id: str, project_id: str, payload: dict[str, Any]) -> str | None:
    baseline = payload.get("baseline_version_id")
    if baseline in (None, ""):
        return None
    with store.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        skill_id = conn.execute(
            text("SELECT skill_id::text FROM skill_versions WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": str(baseline)},
        ).scalar()
    if skill_id is None or str(skill_id) != str(payload.get("skill_id")):
        raise SkillEvalError("baseline_unrelated")
    return str(baseline)


def _candidate_bytes(store: ArtifactStore, *, owner_id: str, project_id: str, version_id: str) -> bytes:
    with store.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        artifact_id = conn.execute(
            text("SELECT bundle_artifact_id::text FROM skill_versions WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": version_id},
        ).scalar()
    if artifact_id is None:
        raise SkillEvalError("version_missing")
    data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(artifact_id))
    return data


def _grade_case(
    candidate_files: dict[str, str],
    suite_files: dict[str, str],
    contract: str,
    *,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aether-skill-") as temporary:
        root = Path(temporary)
        candidate = root / "candidate"
        suite = root / "suite"
        _write_tree(candidate, candidate_files)
        _write_tree(suite, suite_files)
        candidate_sha = export_candidate(candidate)["candidate_sha256"]
        suite_sha = export_candidate(suite)["candidate_sha256"]
        return grade(
            candidate,
            suite,
            suite_sha256=suite_sha,
            contract_text=contract,
            contract_sha256=text_sha256(contract),
            candidate_sha256=candidate_sha,
            timeout_seconds=timeout_seconds,
        )


def _write_tree(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o644)


def _record(
    verifier: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    payload: dict[str, Any],
    outcome: dict[str, Any],
) -> dict[str, Any]:
    report_store = ArtifactStore(store.root, verifier)
    record_evaluation(
        verifier,
        owner_id=owner_id,
        project_id=project_id,
        candidate_version_id=str(payload["candidate_version_id"]),
        dataset_sha256=str(outcome["dataset_sha256"]),
        purpose="development",
        split=str(outcome["split"]),
        metrics=outcome["metrics"],
        synthetic=bool(outcome["synthetic"]),
        report_store=report_store,
        evaluation_id=str(payload["evaluation_id"]),
        baseline_version_id=None if outcome.get("baseline_version_id") is None else str(outcome["baseline_version_id"]),
    )
    return judge(outcome["metrics"], synthetic=bool(outcome["synthetic"]))


def _record_blocked(
    verifier: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    payload: dict[str, Any],
    code: str,
) -> None:
    evaluation_id = str(payload["evaluation_id"])
    if _already_recorded(verifier, owner_id=owner_id, project_id=project_id, evaluation_id=evaluation_id):
        return
    dataset_sha = _dataset_sha(store, owner_id=owner_id, project_id=project_id, artifact_id=str(payload.get("dataset_manifest_id") or ""))
    report_store = ArtifactStore(store.root, verifier)
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        identity = str(conn.execute(text("SELECT current_user")).scalar_one())
        if identity != "aether_verifier":
            raise SkillEvalError("grader_required")
        report = json.dumps(
            {
                "kind": "skill_evaluation_report",
                "evaluation_id": evaluation_id,
                "candidate_version_id": payload.get("candidate_version_id"),
                "status": "REJECTED",
                "error_code": code,
                "evaluator_identity": identity,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        stored = report_store.put(
            owner_id=owner_id,
            project_id=project_id,
            data=report,
            media_type="application/json",
            classification="skill_evaluation_report",
            conn=conn,
        )
        baseline = _persistable_baseline(conn, project_id=project_id, version_id=payload.get("baseline_version_id"))
        conn.execute(
            text(
                "INSERT INTO evaluation_runs (id, project_id, purpose, dataset_manifest_sha256, model_manifest, "
                "candidate_version_id, baseline_version_id, configuration, status, metrics, report_artifact_id, evaluator_identity) "
                "VALUES (:id, :project_id, 'development', :dataset, CAST(:model AS jsonb), :candidate, :baseline, CAST(:configuration AS jsonb), "
                "'REJECTED', NULL, :report, :identity)"
            ),
            {
                "id": evaluation_id,
                "project_id": project_id,
                "dataset": dataset_sha,
                "model": json.dumps({"scripted": True}),
                "candidate": payload.get("candidate_version_id"),
                "baseline": baseline,
                "configuration": json.dumps({"kind": "skill.evaluate", "error_code": code, "report_sha256": stored["sha256"]}, ensure_ascii=False),
                "report": stored["artifact_id"],
                "identity": identity,
            },
        )


def _persistable_baseline(conn: Any, *, project_id: str, version_id: object) -> str | None:
    if not isinstance(version_id, str) or not version_id:
        return None
    found = conn.execute(
        text("SELECT 1 FROM skill_versions WHERE project_id = :project_id AND id = :id"),
        {"project_id": project_id, "id": version_id},
    ).first()
    return version_id if found is not None else None


def _dataset_sha(store: ArtifactStore, *, owner_id: str, project_id: str, artifact_id: str) -> str:
    if not artifact_id:
        return "0" * 64
    try:
        data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
    except ArtifactError:
        return "0" * 64
    return _sha(data)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stored_verdict(
    verifier: Database,
    *,
    owner_id: str,
    project_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT status, metrics FROM evaluation_runs WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": payload["evaluation_id"]},
        ).mappings().first()
    if row is None or payload.get("candidate_version_id") is None:
        return None
    metrics = row["metrics"]
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    if not isinstance(metrics, dict):
        return {"decision": "REJECTED", "label": "not_synthetic", "promoted": False, "learned": False}
    return judge(metrics, synthetic=bool(metrics.get("synthetic")))


def _already_recorded(verifier: Database, *, owner_id: str, project_id: str, evaluation_id: str) -> bool:
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT 1 FROM evaluation_runs WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": evaluation_id},
        ).first()
    return found is not None


def _finish_command(verifier: Database, *, owner_id: str, project_id: str, command_id: str, status: str) -> None:
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE command_outbox SET status = :status, "
                "delivered_at = CASE WHEN :status = 'DELIVERED' THEN now() ELSE delivered_at END "
                "WHERE project_id = :project_id AND id = :id AND status = 'PENDING'"
            ),
            {"status": status, "project_id": project_id, "id": command_id},
        )
