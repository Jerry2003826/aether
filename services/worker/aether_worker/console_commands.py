"""Apply owner commands that the API has already accepted. Acceptance is not success."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import bindparam, text

from aether_domain.security import sha256_text
from aether_storage.db import Database
from aether_worker.skill_eval import SkillEvalError, rollback_active

_KINDS = (
    "source.create",
    "source.patch",
    "source.delete",
    "ingestion.create",
    "feedback.record",
    "skill.import",
    "skill.deploy",
    "watch.create",
    "watch.patch",
    "watch.delete",
    "connector.register",
    "coding.create",
)
_VERIFIER_KINDS = ("evaluation.create", "skill.evaluate")
_FORBIDDEN_SCORE_KEYS = {"metrics", "score", "decision", "success_delta", "promoted"}


class ConsoleCommandError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def apply_console_commands(database: Database, *, owner_id: str, project_id: str) -> list[str]:
    applied: list[str] = []
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT id::text AS id, command_key, kind, payload FROM command_outbox "
                "WHERE project_id = :project_id AND kind IN :kinds AND status = 'PENDING' "
                "ORDER BY created_at, command_key FOR UPDATE"
            ).bindparams(bindparam("kinds", expanding=True)),
            {"project_id": project_id, "kinds": list(_KINDS)},
        ).mappings().all()
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
            nested = conn.begin_nested()
            try:
                _apply(conn, owner_id=owner_id, project_id=project_id, outbox_id=str(row["id"]), kind=str(row["kind"]), payload=payload)
                nested.commit()
                status = "DELIVERED"
            except ConsoleCommandError:
                nested.rollback()
                status = "FAILED"
            conn.execute(
                text(
                    "UPDATE command_outbox SET status = :status, "
                    "delivered_at = CASE WHEN :status = 'DELIVERED' THEN now() ELSE delivered_at END "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"status": status, "project_id": project_id, "id": row["id"]},
            )
            applied.append(str(row["command_key"]))
    return applied


def apply_verifier_commands(database: Database, *, owner_id: str, project_id: str) -> list[str]:
    """Queue an evaluation. The verifier identity is the database role, and the row has no score."""
    applied: list[str] = []
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        identity = str(conn.execute(text("SELECT current_user")).scalar_one())
        if identity != "aether_verifier":
            raise ConsoleCommandError("grader_required")
        rows = conn.execute(
            text(
                "SELECT id::text AS id, command_key, kind, payload FROM command_outbox "
                "WHERE project_id = :project_id AND kind IN :kinds AND status = 'PENDING' "
                "ORDER BY created_at, command_key FOR UPDATE"
            ).bindparams(bindparam("kinds", expanding=True)),
            {"project_id": project_id, "kinds": list(_VERIFIER_KINDS)},
        ).mappings().all()
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
            nested = conn.begin_nested()
            try:
                _queue_evaluation(conn, project_id=project_id, kind=str(row["kind"]), payload=payload, identity=identity)
                nested.commit()
                status = "DELIVERED"
            except ConsoleCommandError:
                nested.rollback()
                status = "FAILED"
            conn.execute(
                text(
                    "UPDATE command_outbox SET status = :status, "
                    "delivered_at = CASE WHEN :status = 'DELIVERED' THEN now() ELSE delivered_at END "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"status": status, "project_id": project_id, "id": row["id"]},
            )
            applied.append(str(row["command_key"]))
    return applied


def _apply(conn: Any, *, owner_id: str, project_id: str, outbox_id: str, kind: str, payload: dict[str, Any]) -> None:
    if kind == "source.create":
        _source_create(conn, project_id, payload)
    elif kind == "source.patch":
        _source_patch(conn, project_id, payload)
    elif kind == "source.delete":
        _source_delete(conn, project_id, payload)
    elif kind == "ingestion.create":
        _ingestion_create(conn, project_id, outbox_id, payload)
    elif kind == "feedback.record":
        _feedback(conn, project_id, payload)
    elif kind == "skill.import":
        _skill_import(conn, project_id, payload)
    elif kind == "skill.deploy":
        _skill_deploy(conn, owner_id, project_id, payload)
    elif kind == "watch.create":
        _watch_create(conn, project_id, payload)
    elif kind == "watch.patch":
        _watch_patch(conn, project_id, payload)
    elif kind == "watch.delete":
        _watch_delete(conn, project_id, payload)
    elif kind == "connector.register":
        _connector(conn, project_id, payload)
    elif kind == "coding.create":
        _coding_create(conn, project_id, payload)
    else:
        raise ConsoleCommandError("command_rejected")


def _source_create(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    source_id = str(payload["source_id"])
    if _exists(conn, "SELECT 1 FROM sources WHERE project_id = :project_id AND id = :id", project_id, source_id):
        return
    conn.execute(
        text(
            "INSERT INTO sources (id, project_id, kind, locator, canonical_locator_hash, approved_config, status) "
            "VALUES (:id, :project_id, :kind, :locator, :digest, CAST(:config AS jsonb), 'ACTIVE')"
        ),
        {
            "id": source_id,
            "project_id": project_id,
            "kind": payload["kind"],
            "locator": payload["locator"],
            "digest": payload["canonical_locator_hash"],
            "config": json.dumps(
                {
                    "allowed_domains": payload.get("allowed_domains") or [],
                    "fetch_interval_seconds": int(payload.get("fetch_interval_seconds") or 3600),
                },
                ensure_ascii=False,
            ),
        },
    )


def _source_patch(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    current = conn.execute(
        text("SELECT version, approved_config, status FROM sources WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
        {"project_id": project_id, "id": payload["source_id"]},
    ).mappings().first()
    if current is None or str(current["version"]) != str(payload["expected_version"]):
        raise ConsoleCommandError("version_conflict")
    config = current["approved_config"] if isinstance(current["approved_config"], dict) else json.loads(current["approved_config"])
    if "allowed_domains" in payload:
        config["allowed_domains"] = payload["allowed_domains"]
    if "fetch_interval_seconds" in payload:
        config["fetch_interval_seconds"] = int(payload["fetch_interval_seconds"])
    status = payload.get("status") or current["status"]
    updated = conn.execute(
        text(
            "UPDATE sources SET approved_config = CAST(:config AS jsonb), status = :status, version = version + 1 "
            "WHERE project_id = :project_id AND id = :id AND version = :version AND deleted_at IS NULL"
        ),
        {
            "config": json.dumps(config, ensure_ascii=False),
            "status": status,
            "project_id": project_id,
            "id": payload["source_id"],
            "version": current["version"],
        },
    ).rowcount
    if updated != 1:
        raise ConsoleCommandError("version_conflict")


def _source_delete(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    updated = conn.execute(
        text(
            "UPDATE sources SET deleted_at = now(), status = 'DELETED', version = version + 1 "
            "WHERE project_id = :project_id AND id = :id AND version = :version AND deleted_at IS NULL"
        ),
        {"project_id": project_id, "id": payload["source_id"], "version": payload["expected_version"]},
    ).rowcount
    if updated != 1 and not _exists(
        conn,
        "SELECT 1 FROM sources WHERE project_id = :project_id AND id = :id AND deleted_at IS NOT NULL",
        project_id,
        str(payload["source_id"]),
    ):
        raise ConsoleCommandError("version_conflict")


def _ingestion_create(conn: Any, project_id: str, outbox_id: str, payload: dict[str, Any]) -> None:
    if not _exists(
        conn,
        "SELECT 1 FROM sources WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL",
        project_id,
        str(payload["source_id"]),
    ):
        raise ConsoleCommandError("source_missing")
    if _exists(conn, "SELECT 1 FROM ingestion_jobs WHERE project_id = :project_id AND command_id = :id", project_id, outbox_id):
        return
    conn.execute(
        text(
            "INSERT INTO ingestion_jobs (id, project_id, source_id, command_id, status) "
            "VALUES (:id, :project_id, :source_id, :command_id, 'QUEUED')"
        ),
        {"id": payload["ingestion_id"], "project_id": project_id, "source_id": payload["source_id"], "command_id": outbox_id},
    )


def _feedback(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    conn.execute(
        text(
            "INSERT INTO feedback_events (project_id, event_key, impression_id, source_id, kind, dwell_seconds, event_time) "
            "VALUES (:project_id, :event_key, :impression_id, :source_id, :kind, :dwell, CAST(:event_time AS timestamptz)) "
            "ON CONFLICT (project_id, event_key) DO NOTHING"
        ),
        {
            "project_id": project_id,
            "event_key": payload["event_id"],
            "impression_id": payload.get("impression_id"),
            "source_id": payload["source_id"],
            "kind": payload["kind"],
            "dwell": payload.get("dwell_seconds"),
            "event_time": payload["event_time"],
        },
    )


def _skill_import(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    if _exists(conn, "SELECT 1 FROM skills WHERE project_id = :project_id AND id = :id", project_id, str(payload["skill_id"])):
        return
    artifact = conn.execute(
        text("SELECT sha256 FROM artifacts WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
        {"project_id": project_id, "id": payload["bundle_artifact_id"]},
    ).mappings().first()
    if artifact is None:
        raise ConsoleCommandError("artifact_missing")
    conn.execute(
        text("INSERT INTO skills (id, project_id, name, description) VALUES (:id, :project_id, :name, :description)"),
        {
            "id": payload["skill_id"],
            "project_id": project_id,
            "name": payload["name"],
            "description": "隔离导入，脚本未执行",
        },
    )
    manifest = {
        "name": payload["name"],
        "version": 1,
        "project_id": project_id,
        "bundle_sha256": artifact["sha256"],
        "training_trace_ids": [],
        "required_tool_ids": [],
        "required_risk": "R1",
        "applicable_task_types": ["research"],
        "status": "QUARANTINED",
        "auto_promote": False,
    }
    conn.execute(
        text(
            "INSERT INTO skill_versions (id, project_id, skill_id, version, bundle_artifact_id, bundle_sha256, manifest, status) "
            "VALUES (:id, :project_id, :skill_id, 1, :artifact_id, :sha, CAST(:manifest AS jsonb), 'QUARANTINED')"
        ),
        {
            "id": payload["version_id"],
            "project_id": project_id,
            "skill_id": payload["skill_id"],
            "artifact_id": payload["bundle_artifact_id"],
            "sha": artifact["sha256"],
            "manifest": json.dumps(manifest),
        },
    )


def _skill_deploy(conn: Any, owner_id: str, project_id: str, payload: dict[str, Any]) -> None:
    action = payload["action"]
    if action == "rollback":
        _rollback(conn, owner_id, project_id, payload)
        return
    if action != "promote":
        raise ConsoleCommandError("deploy_rejected")
    evaluation = conn.execute(
        text(
            "SELECT evaluator_identity, status FROM evaluation_runs "
            "WHERE project_id = :project_id AND id = :id"
        ),
        {"project_id": project_id, "id": payload["evaluation_id"]},
    ).mappings().first()
    version = conn.execute(
        text("SELECT status FROM skill_versions WHERE project_id = :project_id AND id = :id AND skill_id = :skill_id"),
        {"project_id": project_id, "id": payload["target_version_id"], "skill_id": payload["skill_id"]},
    ).mappings().first()
    approval = conn.execute(
        text(
            "SELECT decision FROM approvals WHERE project_id = :project_id AND id = :id "
            "AND decision = 'approved' AND expires_at > now()"
        ),
        {"project_id": project_id, "id": payload["approval_id"]},
    ).first()
    if evaluation is None or evaluation["evaluator_identity"] != "aether_verifier" or version is None or version["status"] != "ELIGIBLE" or approval is None:
        raise ConsoleCommandError("deploy_rejected")
    current = conn.execute(
        text("SELECT version_id::text AS version_id, deployment_version FROM skill_deployments WHERE project_id = :project_id AND skill_id = :skill_id AND slot = 'canary'"),
        {"project_id": project_id, "skill_id": payload["skill_id"]},
    ).mappings().first()
    expected = payload.get("expected_previous_version_id")
    if current is None:
        if expected not in {None, ""} or str(payload["expected_deployment_version"]) != "0":
            raise ConsoleCommandError("version_conflict")
        conn.execute(
            text("UPDATE skill_versions SET status = 'CANARY' WHERE project_id = :project_id AND id = :id AND status = 'ELIGIBLE'"),
            {"project_id": project_id, "id": payload["target_version_id"]},
        )
        conn.execute(
            text(
                "INSERT INTO skill_deployments (project_id, skill_id, slot, version_id, evaluation_id, approval_id, deployment_version, traffic_fraction) "
                "VALUES (:project_id, :skill_id, 'canary', :version_id, :evaluation_id, :approval_id, 1, 0.05)"
            ),
            {
                "project_id": project_id,
                "skill_id": payload["skill_id"],
                "version_id": payload["target_version_id"],
                "evaluation_id": payload["evaluation_id"],
                "approval_id": payload["approval_id"],
            },
        )
        return
    if str(current["deployment_version"]) != str(payload["expected_deployment_version"]) or (expected and current["version_id"] != expected):
        raise ConsoleCommandError("version_conflict")
    raise ConsoleCommandError("deploy_rejected")


def _rollback(conn: Any, owner_id: str, project_id: str, payload: dict[str, Any]) -> None:
    task_id = payload.get("task_id")
    if not task_id:
        raise ConsoleCommandError("deploy_rejected")
    try:
        rollback_active(
            _ConnectionDatabase(conn),
            owner_id=owner_id,
            project_id=project_id,
            skill_id=str(payload["skill_id"]),
            previous_version_id=str(payload["target_version_id"]),
            task_id=str(task_id),
        )
    except SkillEvalError as exc:
        raise ConsoleCommandError(exc.code) from exc


def _watch_create(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    if _exists(conn, "SELECT 1 FROM watches WHERE project_id = :project_id AND id = :id", project_id, str(payload["watch_id"])):
        return
    conn.execute(
        text(
            "INSERT INTO watches (id, project_id, purpose, schedule_id, source_ids, schedule_spec, budget_spec, status) "
            "VALUES (:id, :project_id, :purpose, :schedule_id, CAST(:sources AS jsonb), CAST(:schedule AS jsonb), CAST(:budget AS jsonb), 'DRAFT')"
        ),
        {
            "id": payload["watch_id"],
            "project_id": project_id,
            "purpose": payload["purpose"],
            "schedule_id": payload["schedule_id"],
            "sources": json.dumps(payload["source_ids"]),
            "schedule": json.dumps(payload["schedule"]),
            "budget": json.dumps(payload["budget"]),
        },
    )


def _watch_patch(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    current = conn.execute(
        text("SELECT version, status, schedule_spec FROM watches WHERE project_id = :project_id AND id = :id"),
        {"project_id": project_id, "id": payload["watch_id"]},
    ).mappings().first()
    if current is None or str(current["version"]) != str(payload["expected_version"]):
        raise ConsoleCommandError("version_conflict")
    if payload.get("status") == "ACTIVE":
        raise ConsoleCommandError("schedule_requires_approval")
    schedule = current["schedule_spec"] if isinstance(current["schedule_spec"], dict) else json.loads(current["schedule_spec"])
    if "interval_seconds" in payload:
        schedule["interval_seconds"] = int(payload["interval_seconds"])
        schedule["every_seconds"] = int(payload["interval_seconds"])
    status = payload.get("status") or current["status"]
    if status == "ACTIVE":
        raise ConsoleCommandError("schedule_requires_approval")
    updated = conn.execute(
        text(
            "UPDATE watches SET status = :status, schedule_spec = CAST(:schedule AS jsonb), version = version + 1 "
            "WHERE project_id = :project_id AND id = :id AND version = :version AND status <> 'ACTIVE'"
        ),
        {
            "status": status,
            "schedule": json.dumps(schedule),
            "project_id": project_id,
            "id": payload["watch_id"],
            "version": current["version"],
        },
    ).rowcount
    if updated != 1:
        raise ConsoleCommandError("schedule_requires_approval")


def _watch_delete(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    updated = conn.execute(
        text(
            "UPDATE watches SET status = 'DELETED', version = version + 1 "
            "WHERE project_id = :project_id AND id = :id AND version = :version AND status IN ('DRAFT', 'PAUSED')"
        ),
        {"project_id": project_id, "id": payload["watch_id"], "version": payload["expected_version"]},
    ).rowcount
    if updated != 1:
        raise ConsoleCommandError("schedule_requires_approval")


def _connector(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    if "secret_value" in payload:
        raise ConsoleCommandError("secret_rejected")
    if _exists(conn, "SELECT 1 FROM connector_configs WHERE project_id = :project_id AND id = :id", project_id, str(payload["connector_id"])):
        return
    conn.execute(
        text(
            "INSERT INTO connector_configs (id, project_id, connector_key, transport, reviewed_manifest, manifest_sha256, secret_reference, status) "
            "VALUES (:id, :project_id, :connector_key, :transport, CAST(:manifest AS jsonb), :digest, :secret_reference, 'reviewed')"
        ),
        {
            "id": payload["connector_id"],
            "project_id": project_id,
            "connector_key": payload["connector_key"],
            "transport": payload["transport"],
            "manifest": json.dumps(payload["manifest"], ensure_ascii=False),
            "digest": payload["manifest_sha256"],
            "secret_reference": payload.get("secret_reference"),
        },
    )


def _coding_create(conn: Any, project_id: str, payload: dict[str, Any]) -> None:
    job_id = str(payload["coding_job_id"])
    if _exists(conn, "SELECT 1 FROM coding_jobs WHERE project_id = :project_id AND id = :id", project_id, job_id):
        return
    if not _exists(conn, "SELECT 1 FROM tasks WHERE project_id = :project_id AND id = :id", project_id, str(payload["task_id"])):
        raise ConsoleCommandError("task_missing")
    if not _exists(
        conn,
        "SELECT 1 FROM artifacts WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL",
        project_id,
        str(payload["base_artifact_id"]),
    ):
        raise ConsoleCommandError("artifact_missing")
    arguments = {"objective": payload["objective"], "base_artifact_id": payload["base_artifact_id"]}
    digest = sha256_text(json.dumps(arguments, sort_keys=True, separators=(",", ":")))
    conn.execute(
        text(
            "INSERT INTO capability_grants (id, project_id, task_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
            "VALUES (:id, :project_id, :task_id, 'aether_worker', :token_hash, 'coding:execute', CAST(:scope AS jsonb), now() + interval '1 hour', 'v1')"
        ),
        {
            "id": payload["capability_id"],
            "project_id": project_id,
            "task_id": payload["task_id"],
            "token_hash": sha256_text(job_id),
            "scope": json.dumps({"project_id": project_id}),
        },
    )
    conn.execute(
        text(
            "INSERT INTO operations (id, project_id, task_id, intent_key, tool_id, args_sha256, intent_digest, normalized_arguments, "
            "resource_id, resource_version, idempotency_class, risk, capability_id, state) "
            "VALUES (:id, :project_id, :task_id, :intent_key, 'coding.start', :digest, :digest, CAST(:arguments AS jsonb), "
            ":resource_id, '1', 'idempotent_write', 'R1', :capability_id, 'PREPARED')"
        ),
        {
            "id": payload["operation_id"],
            "project_id": project_id,
            "task_id": payload["task_id"],
            "intent_key": job_id,
            "digest": digest,
            "arguments": json.dumps(arguments, ensure_ascii=False),
            "resource_id": payload["base_artifact_id"],
            "capability_id": payload["capability_id"],
        },
    )
    conn.execute(
        text(
            "INSERT INTO coding_jobs (id, project_id, task_id, operation_id, job_key, state, base_artifact_id, deadline) "
            "VALUES (:id, :project_id, :task_id, :operation_id, :job_key, 'QUEUED', :artifact_id, now() + interval '1 hour')"
        ),
        {
            "id": job_id,
            "project_id": project_id,
            "task_id": payload["task_id"],
            "operation_id": payload["operation_id"],
            "job_key": payload["job_key"],
            "artifact_id": payload["base_artifact_id"],
        },
    )


def _queue_evaluation(conn: Any, *, project_id: str, kind: str, payload: dict[str, Any], identity: str) -> None:
    if _FORBIDDEN_SCORE_KEYS & set(payload):
        raise ConsoleCommandError("score_rejected")
    evaluation_id = str(payload["evaluation_id"])
    if _exists(conn, "SELECT 1 FROM evaluation_runs WHERE project_id = :project_id AND id = :id", project_id, evaluation_id):
        return
    artifact = conn.execute(
        text("SELECT sha256 FROM artifacts WHERE project_id = :project_id AND id = :id AND deleted_at IS NULL"),
        {"project_id": project_id, "id": payload["dataset_manifest_id"]},
    ).mappings().first()
    if artifact is None:
        raise ConsoleCommandError("artifact_missing")
    purpose = "development" if kind == "skill.evaluate" else str(payload["purpose"])
    if kind == "skill.evaluate" and payload.get("purpose") == "release":
        raise ConsoleCommandError("dataset_isolated")
    conn.execute(
        text(
            "INSERT INTO evaluation_runs (id, project_id, purpose, dataset_manifest_sha256, model_manifest, "
            "candidate_version_id, configuration, status, metrics, evaluator_identity) "
            "VALUES (:id, :project_id, :purpose, :dataset, CAST(:model AS jsonb), :candidate, CAST(:configuration AS jsonb), "
            "'QUEUED', NULL, :identity)"
        ),
        {
            "id": evaluation_id,
            "project_id": project_id,
            "purpose": purpose,
            "dataset": artifact["sha256"],
            "model": json.dumps({"live": "not_run"}),
            "candidate": payload.get("candidate_version_id"),
            "configuration": json.dumps(payload.get("configuration") or {"kind": kind}, ensure_ascii=False),
            "identity": identity,
        },
    )


def _exists(conn: Any, statement: str, project_id: str, row_id: str) -> bool:
    return conn.execute(text(statement), {"project_id": project_id, "id": row_id}).first() is not None


class _ConnectionDatabase:
    """Reuse the open worker transaction. rollback_active must not commit a second one."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def transaction(self, *, owner_id: str, project_id: str) -> Any:
        return _OpenTransaction(self._connection)


class _OpenTransaction:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __enter__(self) -> Any:
        return self._connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False


def new_id() -> str:
    return str(uuid.uuid4())
