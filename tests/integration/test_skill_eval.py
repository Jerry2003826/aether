from datetime import UTC, datetime, timedelta

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.skill_eval import SkillEvalError, apply_verdict, approve_canary, judge, record_evaluation, rollback_active
from tests.integration.conftest import insert_task
from tests.integration.test_auth import ORIGIN, _bootstrap

SHA = "a" * 64


def _session(client, key: str):
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    headers = {**ORIGIN, "X-CSRF-Token": body["csrf_token"], "Idempotency-Key": key}
    project = client.post("/v1/projects", headers=headers, json={"name": "技能评测"})
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"]


def _version(database: Database, store: ArtifactStore, owner_id: str, project_id: str, status: str) -> tuple[str, str]:
    stored = store.put(owner_id=owner_id, project_id=project_id, data=b"{}", media_type="application/json")
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        skill_id = conn.execute(
            text("INSERT INTO skills (project_id, name, description) VALUES (:project_id, :name, '说明') RETURNING id::text"),
            {"project_id": project_id, "name": f"skill-{status}-{stored['artifact_id'][:8]}"},
        ).scalar_one()
        version_id = conn.execute(
            text(
                "INSERT INTO skill_versions (project_id, skill_id, version, bundle_artifact_id, bundle_sha256, manifest, status) "
                "VALUES (:project_id, :skill_id, 1, :artifact_id, :sha, '{}'::jsonb, :status) RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "skill_id": skill_id,
                "artifact_id": stored["artifact_id"],
                "sha": stored["sha256"],
                "status": status,
            },
        ).scalar_one()
    return str(skill_id), str(version_id)


def test_actor_cannot_write_scores_and_holdout_stays_out(client, role_urls, tmp_path) -> None:
    owner_id, project_id = _session(client, "skill-eval-iso")
    api = Database(role_urls["aether_api"])
    with pytest.raises(DBAPIError) as denied:
        with api.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(text("INSERT INTO evaluation_runs (project_id, purpose, dataset_manifest_sha256, model_manifest, configuration, status) VALUES (:project_id, 'development', :sha, '{}'::jsonb, '{}'::jsonb, 'REJECTED')"), {"project_id": project_id, "sha": SHA})
    assert isinstance(denied.value.orig, InsufficientPrivilege)
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", worker)
    _skill, version_id = _version(worker, store, owner_id, project_id, "EVALUATING")
    verifier = Database(role_urls["aether_verifier"])
    with pytest.raises(SkillEvalError) as isolated:
        record_evaluation(
            verifier,
            owner_id=owner_id,
            project_id=project_id,
            candidate_version_id=version_id,
            dataset_sha256=SHA,
            purpose="development",
            split="holdout",
            metrics={"families": 30, "ci_lower": 0.1, "success_delta": 0.1, "cost_ratio": 1},
            synthetic=False,
        )
    assert isolated.value.code == "dataset_isolated"


def test_weak_or_synthetic_candidates_are_not_promoted() -> None:
    assert judge({"families": 3, "ci_lower": 0.2, "success_delta": 0.2, "cost_ratio": 1}, synthetic=False)["decision"] == "INSUFFICIENT_EVIDENCE"
    assert judge({"families": 30, "ci_lower": -0.01, "success_delta": 0.2, "cost_ratio": 1}, synthetic=False)["promoted"] is False
    bad = judge({"families": 30, "ci_lower": 0.1, "success_delta": 0.01, "cost_ratio": 1, "security_failure": True}, synthetic=False)
    assert bad["decision"] == "REJECTED"
    assert bad["promoted"] is False
    synthetic = judge({"families": 30, "ci_lower": 0.2, "success_delta": 0.2, "cost_ratio": 1}, synthetic=True)
    assert synthetic["label"] == "synthetic_control"
    assert synthetic["learned"] is False
    assert synthetic["promoted"] is False


def test_only_owner_can_canary_and_rollback_keeps_the_pin(client, role_urls, tmp_path) -> None:
    owner_id, project_id = _session(client, "skill-eval-canary")
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", worker)
    skill_id, current_id = _version(worker, store, owner_id, project_id, "ELIGIBLE")
    stored = store.put(owner_id=owner_id, project_id=project_id, data=b"{}", media_type="application/json")
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        previous_id = str(
            conn.execute(
                text(
                    "INSERT INTO skill_versions (project_id, skill_id, version, bundle_artifact_id, bundle_sha256, manifest, status) "
                    "VALUES (:project_id, :skill_id, 2, :artifact_id, :sha, '{}'::jsonb, 'ACTIVE') RETURNING id::text"
                ),
                {"project_id": project_id, "skill_id": skill_id, "artifact_id": stored["artifact_id"], "sha": stored["sha256"]},
            ).scalar_one()
        )
    task_id = insert_task(worker, owner_id, project_id, "固定版本")
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, '{}'::jsonb, :sha, :owner, now())"
            ),
            {"project_id": project_id, "task_id": task_id, "sha": SHA, "owner": owner_id},
        )
        approval_id = conn.execute(
            text(
                "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, decision, owner_id, expires_at, decided_at) "
                "VALUES (:project_id, :task_id, :digest, '{}'::jsonb, 'approved', :owner, :expires, now()) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "digest": SHA, "owner": owner_id, "expires": datetime.now(UTC) + timedelta(hours=1)},
        ).scalar_one()
    verifier = Database(role_urls["aether_verifier"])
    evaluation_id = record_evaluation(
        verifier,
        owner_id=owner_id,
        project_id=project_id,
        candidate_version_id=current_id,
        dataset_sha256=SHA,
        purpose="development",
        split="train",
        metrics={"families": 4, "ci_lower": 0.2, "success_delta": 0.2, "cost_ratio": 1},
        synthetic=True,
    )
    status = apply_verdict(worker, owner_id=owner_id, project_id=project_id, version_id=current_id, verdict=judge({"families": 4}, synthetic=True))
    assert status == "REJECTED"
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(text("UPDATE skill_versions SET status = 'ELIGIBLE' WHERE project_id = :project_id AND id = :id"), {"project_id": project_id, "id": current_id})
    with pytest.raises(SkillEvalError) as stranger:
        approve_canary(
            worker,
            owner_id=owner_id,
            actor_id="00000000-0000-4000-8000-000000000099",
            project_id=project_id,
            skill_id=skill_id,
            version_id=current_id,
            evaluation_id=evaluation_id,
            approval_id=str(approval_id),
            task_id=task_id,
        )
    assert stranger.value.code == "owner_required"
    pinned = approve_canary(
        worker,
        owner_id=owner_id,
        actor_id=owner_id,
        project_id=project_id,
        skill_id=skill_id,
        version_id=current_id,
        evaluation_id=evaluation_id,
        approval_id=str(approval_id),
        task_id=task_id,
    )
    assert pinned["pinned_version_id"] == current_id
    rolled = rollback_active(worker, owner_id=owner_id, project_id=project_id, skill_id=skill_id, previous_version_id=previous_id, task_id=task_id)
    assert rolled["atomic"] is True
    assert rolled["deployment_version_id"] == previous_id
    assert rolled["pinned_version_id"] == current_id


def test_rollback_is_atomic_when_the_second_write_fails(client, role_urls, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, project_id = _session(client, "skill-eval-atomic")
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", worker)
    skill_id, current_id = _version(worker, store, owner_id, project_id, "ELIGIBLE")
    stored = store.put(owner_id=owner_id, project_id=project_id, data=b"{}", media_type="application/json")
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        previous_id = str(
            conn.execute(
                text(
                    "INSERT INTO skill_versions (project_id, skill_id, version, bundle_artifact_id, bundle_sha256, manifest, status) "
                    "VALUES (:project_id, :skill_id, 2, :artifact_id, :sha, '{}'::jsonb, 'ACTIVE') RETURNING id::text"
                ),
                {"project_id": project_id, "skill_id": skill_id, "artifact_id": stored["artifact_id"], "sha": stored["sha256"]},
            ).scalar_one()
        )
    task_id = insert_task(worker, owner_id, project_id, "原子回滚")
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, '{}'::jsonb, :sha, :owner, now())"
            ),
            {"project_id": project_id, "task_id": task_id, "sha": SHA, "owner": owner_id},
        )
        approval_id = conn.execute(
            text(
                "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, decision, owner_id, expires_at, decided_at) "
                "VALUES (:project_id, :task_id, :digest, '{}'::jsonb, 'approved', :owner, :expires, now()) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "digest": SHA, "owner": owner_id, "expires": datetime.now(UTC) + timedelta(hours=1)},
        ).scalar_one()
    verifier = Database(role_urls["aether_verifier"])
    evaluation_id = record_evaluation(
        verifier,
        owner_id=owner_id,
        project_id=project_id,
        candidate_version_id=current_id,
        dataset_sha256=SHA,
        purpose="development",
        split="train",
        metrics={"families": 4, "ci_lower": 0.2, "success_delta": 0.2, "cost_ratio": 1},
        synthetic=True,
    )
    approve_canary(
        worker,
        owner_id=owner_id,
        actor_id=owner_id,
        project_id=project_id,
        skill_id=skill_id,
        version_id=current_id,
        evaluation_id=evaluation_id,
        approval_id=str(approval_id),
        task_id=task_id,
    )
    original = Connection.execute

    def execute(self: Connection, statement: object, *args: object, **kwargs: object) -> object:
        if "SET status = 'ROLLED_BACK'" in str(statement):
            raise RuntimeError("injected")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            rollback_active(worker, owner_id=owner_id, project_id=project_id, skill_id=skill_id, previous_version_id=previous_id, task_id=task_id)
    finally:
        monkeypatch.undo()
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        pointer = conn.execute(
            text("SELECT version_id::text FROM skill_deployments WHERE project_id = :project_id AND skill_id = :skill_id AND slot = 'canary'"),
            {"project_id": project_id, "skill_id": skill_id},
        ).scalar_one()
        status = conn.execute(
            text("SELECT status FROM skill_versions WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": current_id},
        ).scalar_one()
        deployment_version = conn.execute(
            text("SELECT deployment_version FROM skill_deployments WHERE project_id = :project_id AND skill_id = :skill_id AND slot = 'canary'"),
            {"project_id": project_id, "skill_id": skill_id},
        ).scalar_one()
    assert pointer == current_id
    assert status == "CANARY"
    assert int(deployment_version) == 1
