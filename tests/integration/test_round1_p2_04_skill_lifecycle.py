"""System audit round 1, P2-04: rolling a canary back through the API, and what a promotion must be bound to.

INV-LEARN-01: an owner can roll a canary skill back to the previous version through the public API.
INV-LEARN-02: a promotion uses an approval issued for this promotion and not yet used, and a passed
evaluation of this candidate version.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_storage.operations import OperationLedger
from aether_worker.service import route_pending
from aether_worker.skill_eval import record_evaluation
from tests.integration.test_auth import ORIGIN, _bootstrap

GOOD = {"families": 40, "ci_lower": 0.02, "success_delta": 0.10, "cost_ratio": 1.0}


def _key() -> str:
    return f"round1-{uuid.uuid4().hex}"


def _project(client) -> tuple[str, str, dict[str, str]]:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    created = client.post("/v1/projects", headers={**headers, "Idempotency-Key": _key()}, json={"name": "技能生命周期"})
    assert created.status_code == 201, created.text
    return session["owner_id"], created.json()["id"], headers


def _rows(database: Database, owner: str, project_id: str, sql: str, **params: Any) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner, project_id=project_id) as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _status(database: Database, owner: str, project_id: str, command_id: str) -> str:
    return _rows(database, owner, project_id, "SELECT status FROM command_outbox WHERE command_key = :k", k=command_id)[0]["status"]


def _apply(client, role_urls) -> None:
    worker = Database(role_urls["aether_worker"])
    route_pending(
        Database(role_urls["aether_dispatcher"]),
        worker,
        Database(role_urls["aether_verifier"]),
        ArtifactStore(client.app.state.settings.artifact_root, worker),
    )


def _canary_setup(client, role_urls):
    owner, project_id, headers = _project(client)
    worker = Database(role_urls["aether_worker"])
    bundle = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": _key()},
        files={"file": ("SKILL.md", "---\nname: demo\ndescription: demo\n---\n正文\n".encode(), "text/plain")},
    )
    assert bundle.status_code == 201, bundle.text
    imported = client.post(f"/v1/projects/{project_id}/skills", headers={**headers, "Idempotency-Key": _key()}, json={"bundle_artifact_id": bundle.json()["artifact_id"]})
    assert imported.status_code == 202, imported.text
    _apply(client, role_urls)
    skill_id = imported.json()["resource_id"]
    v1 = _rows(worker, owner, project_id, "SELECT id::text AS id, bundle_artifact_id::text AS b, bundle_sha256 AS s, manifest FROM skill_versions WHERE skill_id = :s", s=skill_id)[0]
    v2 = str(uuid.uuid4())
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO skill_versions (id, project_id, skill_id, version, parent_version_id, bundle_artifact_id, bundle_sha256, manifest, status) "
                "VALUES (:id, :p, :s, 2, :parent, :b, :sha, CAST(:m AS jsonb), 'ELIGIBLE')"
            ),
            {"id": v2, "p": project_id, "s": skill_id, "parent": v1["id"], "b": v1["b"], "sha": v1["s"], "m": '{"name": "demo", "version": 2}'},
        )
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": _key()}, json={"goal": "审批载体"})
    task_id = created.json()["resource_id"]
    return owner, project_id, headers, worker, skill_id, v1["id"], v2, task_id


def _approved(role_urls, owner: str, project_id: str, task_id: str, intent: dict[str, Any]) -> str:
    worker = Database(role_urls["aether_worker"])
    approval = OperationLedger(worker).request_approval(owner_id=owner, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    digest = _rows(worker, owner, project_id, "SELECT intent_digest FROM approvals WHERE id = :id", id=approval)[0]["intent_digest"]
    OperationLedger(Database(role_urls["aether_api"])).decide(owner_id=owner, project_id=project_id, task_id=task_id, approval_id=approval, decision="approved", expected_intent_hash=digest)
    return approval


def _deploy(client, project_id: str, skill_id: str, headers: dict[str, str], match: str, body: dict[str, Any]) -> str:
    response = client.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/deployments",
        headers={**headers, "Idempotency-Key": _key(), "If-Match": match},
        json=body,
    )
    assert response.status_code == 202, response.text
    return response.json()["command_id"]


def _canary(worker: Database, owner: str, project_id: str, skill_id: str) -> list[dict[str, Any]]:
    return _rows(worker, owner, project_id, "SELECT version_id::text AS v FROM skill_deployments WHERE skill_id = :s AND slot = 'canary'", s=skill_id)


def _promoted(client, role_urls):
    owner, project_id, headers, worker, skill_id, v1, v2, task_id = _canary_setup(client, role_urls)
    evaluation = record_evaluation(
        Database(role_urls["aether_verifier"]), owner_id=owner, project_id=project_id, candidate_version_id=v2,
        dataset_sha256="e" * 64, purpose="validation", split="dev", metrics=GOOD, synthetic=False,
    )
    approval = _approved(role_urls, owner, project_id, task_id, {"action": "promote", "skill_id": skill_id, "version_id": v2})
    promote = _deploy(client, project_id, skill_id, headers, "0", {
        "action": "promote", "target_version_id": v2, "evaluation_id": evaluation, "expected_previous_version_id": None,
        "approval_id": approval, "report_sha256": "a" * 64,
    })
    _apply(client, role_urls)
    assert _status(worker, owner, project_id, promote) == "DELIVERED"
    return owner, project_id, headers, worker, skill_id, v1, v2, evaluation, approval


def test_owner_can_roll_a_canary_back_through_the_api(client, role_urls) -> None:
    owner, project_id, headers, worker, skill_id, v1, v2, evaluation, approval = _promoted(client, role_urls)
    rollback = _deploy(client, project_id, skill_id, headers, "1", {
        "action": "rollback", "target_version_id": v1, "evaluation_id": evaluation, "expected_previous_version_id": v2,
        "approval_id": approval, "report_sha256": "a" * 64,
    })
    _apply(client, role_urls)
    canary = _canary(worker, owner, project_id, skill_id)
    detail = f"回滚命令状态={_status(worker, owner, project_id, rollback)}，canary 仍指向 {canary}（v1={v1} v2={v2}）"
    assert _status(worker, owner, project_id, rollback) == "DELIVERED", detail
    assert canary == [{"v": v1}], detail


def test_rollback_that_does_not_name_the_live_canary_is_rejected(client, role_urls) -> None:
    owner, project_id, headers, worker, skill_id, v1, v2, evaluation, approval = _promoted(client, role_urls)
    stale = _deploy(client, project_id, skill_id, headers, "1", {
        "action": "rollback", "target_version_id": v1, "evaluation_id": evaluation, "expected_previous_version_id": v1,
        "approval_id": approval, "report_sha256": "a" * 64,
    })
    unrelated = _deploy(client, project_id, skill_id, headers, "1", {
        "action": "rollback", "target_version_id": str(uuid.uuid4()), "evaluation_id": evaluation, "expected_previous_version_id": v2,
        "approval_id": approval, "report_sha256": "a" * 64,
    })
    _apply(client, role_urls)
    assert _status(worker, owner, project_id, stale) == "FAILED"
    assert _status(worker, owner, project_id, unrelated) == "FAILED"
    assert _canary(worker, owner, project_id, skill_id) == [{"v": v2}]


def test_promotion_requires_an_approval_and_evaluation_for_this_version(client, role_urls) -> None:
    owner, project_id, headers, worker, skill_id, v1, v2, task_id = _canary_setup(client, role_urls)
    unrelated_eval = record_evaluation(
        Database(role_urls["aether_verifier"]), owner_id=owner, project_id=project_id, candidate_version_id=v1,
        dataset_sha256="e" * 64, purpose="validation", split="dev", metrics={"families": 1}, synthetic=False,
    )
    approval = _approved(role_urls, owner, project_id, task_id, {"action": "send_email", "to": "someone@example.test"})
    promote = _deploy(client, project_id, skill_id, headers, "0", {
        "action": "promote", "target_version_id": v2, "evaluation_id": unrelated_eval, "expected_previous_version_id": None,
        "approval_id": approval, "report_sha256": "0" * 64,
    })
    _apply(client, role_urls)
    status = _status(worker, owner, project_id, promote)
    canary = _canary(worker, owner, project_id, skill_id)
    detail = f"用一个发邮件的审批、一个评测别的版本且证据不足的评测去晋升 v2：命令={status} canary={canary}"
    assert status == "FAILED", detail
    assert canary == [], detail


def test_each_half_of_the_promotion_gate_is_checked_on_its_own(client, role_urls) -> None:
    owner, project_id, headers, worker, skill_id, v1, v2, task_id = _canary_setup(client, role_urls)
    verifier = Database(role_urls["aether_verifier"])
    passed = record_evaluation(verifier, owner_id=owner, project_id=project_id, candidate_version_id=v2, dataset_sha256="e" * 64, purpose="validation", split="dev", metrics=GOOD, synthetic=False)
    weak = record_evaluation(verifier, owner_id=owner, project_id=project_id, candidate_version_id=v2, dataset_sha256="e" * 64, purpose="validation", split="dev", metrics={"families": 1}, synthetic=False)
    right = _approved(role_urls, owner, project_id, task_id, {"action": "promote", "skill_id": skill_id, "version_id": v2})
    wrong_version = _approved(role_urls, owner, project_id, task_id, {"action": "promote", "skill_id": skill_id, "version_id": v1})
    commands = [
        _deploy(client, project_id, skill_id, headers, "0", {
            "action": "promote", "target_version_id": v2, "evaluation_id": weak, "expected_previous_version_id": None,
            "approval_id": right, "report_sha256": "a" * 64,
        }),
        _deploy(client, project_id, skill_id, headers, "0", {
            "action": "promote", "target_version_id": v2, "evaluation_id": passed, "expected_previous_version_id": None,
            "approval_id": wrong_version, "report_sha256": "a" * 64,
        }),
    ]
    _apply(client, role_urls)
    assert [_status(worker, owner, project_id, command) for command in commands] == ["FAILED", "FAILED"]
    assert _canary(worker, owner, project_id, skill_id) == []


def test_an_approval_that_already_authorized_a_deployment_cannot_be_used_again(client, role_urls, aether_admin) -> None:
    owner, project_id, headers, worker, skill_id, v1, v2, evaluation, approval = _promoted(client, role_urls)
    ledger = _rows(
        worker, owner, project_id,
        "SELECT operation.tool_id, operation.state FROM approvals AS approval "
        "JOIN operations AS operation ON operation.project_id = approval.project_id AND operation.id = approval.consumed_by_operation_id "
        "WHERE approval.id = :id AND operation.approval_id = approval.id",
        id=approval,
    )
    assert ledger == [{"tool_id": "skill.promote", "state": "CONFIRMED"}]
    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute("DELETE FROM skill_deployments WHERE skill_id = %s", (skill_id,))
        conn.execute("UPDATE skill_versions SET status = 'ELIGIBLE' WHERE id = %s", (v2,))
    replay = _deploy(client, project_id, skill_id, headers, "0", {
        "action": "promote", "target_version_id": v2, "evaluation_id": evaluation, "expected_previous_version_id": None,
        "approval_id": approval, "report_sha256": "a" * 64,
    })
    _apply(client, role_urls)
    assert _status(worker, owner, project_id, replay) == "FAILED"
    assert _canary(worker, owner, project_id, skill_id) == []
