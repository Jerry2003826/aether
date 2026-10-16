"""P2-04 · 技能回滚走公开 API 永远失败；晋升闸门不绑定审批与评测。

不变量 INV-LEARN-01：已上线（canary）的技能可以经 owner 审批回滚到上一版本；
不变量 INV-LEARN-02：晋升只能使用为这次晋升签发、尚未消费的审批，以及针对这个候选版本、已通过的评测。
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from aether_storage.db import Database
from aether_storage.operations import OperationLedger
from aether_worker.service import route_pending
from aether_worker.skill_eval import record_evaluation
from audit_helpers import key, project, rows, store_for

GOOD = {"families": 40, "ci_lower": 0.02, "success_delta": 0.10, "cost_ratio": 1.0}


def _status(database: Database, owner: str, project_id: str, command_id: str) -> str:
    return rows(database, owner, project_id, "SELECT status FROM command_outbox WHERE command_key = :k", k=command_id)[0]["status"]


def _apply(client, role_urls) -> None:
    route_pending(
        Database(role_urls["aether_dispatcher"]),
        Database(role_urls["aether_worker"]),
        Database(role_urls["aether_verifier"]),
        store_for(client, role_urls),
    )


def _canary_setup(client, role_urls):
    owner, project_id, headers = project(client)
    worker = Database(role_urls["aether_worker"])
    bundle = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("SKILL.md", "---\nname: demo\ndescription: demo\n---\n正文\n".encode(), "text/plain")},
    )
    assert bundle.status_code == 201, bundle.text
    imported = client.post(f"/v1/projects/{project_id}/skills", headers={**headers, "Idempotency-Key": key()}, json={"bundle_artifact_id": bundle.json()["artifact_id"]})
    assert imported.status_code == 202, imported.text
    _apply(client, role_urls)
    skill_id = imported.json()["resource_id"]
    v1 = rows(worker, owner, project_id, "SELECT id::text AS id, bundle_artifact_id::text AS b, bundle_sha256 AS s, manifest FROM skill_versions WHERE skill_id = :s", s=skill_id)[0]
    v2 = str(uuid.uuid4())
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO skill_versions (id, project_id, skill_id, version, parent_version_id, bundle_artifact_id, bundle_sha256, manifest, status) "
                "VALUES (:id, :p, :s, 2, :parent, :b, :sha, CAST(:m AS jsonb), 'ELIGIBLE')"
            ),
            {"id": v2, "p": project_id, "s": skill_id, "parent": v1["id"], "b": v1["b"], "sha": v1["s"], "m": '{"name": "demo", "version": 2}'},
        )
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "审批载体"})
    task_id = created.json()["resource_id"]
    return owner, project_id, headers, worker, skill_id, v1["id"], v2, task_id


def test_owner_can_roll_a_canary_back_through_the_api(client, role_urls) -> None:
    owner, project_id, headers, worker, skill_id, v1, v2, task_id = _canary_setup(client, role_urls)
    evaluation = record_evaluation(
        Database(role_urls["aether_verifier"]), owner_id=owner, project_id=project_id, candidate_version_id=v2,
        dataset_sha256="e" * 64, purpose="validation", split="dev", metrics=GOOD, synthetic=False,
    )
    intent = {"action": "promote", "skill_id": skill_id, "version_id": v2}
    approval = OperationLedger(worker).request_approval(owner_id=owner, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    digest = rows(worker, owner, project_id, "SELECT intent_digest FROM approvals WHERE id = :id", id=approval)[0]["intent_digest"]
    OperationLedger(Database(role_urls["aether_api"])).decide(owner_id=owner, project_id=project_id, task_id=task_id, approval_id=approval, decision="approved", expected_intent_hash=digest)
    promote = client.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/deployments",
        headers={**headers, "Idempotency-Key": key(), "If-Match": "0"},
        json={"action": "promote", "target_version_id": v2, "evaluation_id": evaluation, "expected_previous_version_id": None, "approval_id": approval, "report_sha256": "a" * 64},
    )
    assert promote.status_code == 202, promote.text
    _apply(client, role_urls)
    assert _status(worker, owner, project_id, promote.json()["command_id"]) == "DELIVERED"
    rollback = client.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/deployments",
        headers={**headers, "Idempotency-Key": key(), "If-Match": "1"},
        json={"action": "rollback", "target_version_id": v1, "evaluation_id": evaluation, "expected_previous_version_id": v2, "approval_id": approval, "report_sha256": "a" * 64},
    )
    assert rollback.status_code == 202, rollback.text
    _apply(client, role_urls)
    canary = rows(worker, owner, project_id, "SELECT version_id::text AS v FROM skill_deployments WHERE skill_id = :s AND slot = 'canary'", s=skill_id)
    detail = f"回滚命令状态={_status(worker, owner, project_id, rollback.json()['command_id'])}，canary 仍指向 {canary}（v1={v1} v2={v2}）"
    assert _status(worker, owner, project_id, rollback.json()["command_id"]) == "DELIVERED", detail
    assert canary == [{"v": v1}], detail


def test_promotion_requires_an_approval_and_evaluation_for_this_version(client, role_urls) -> None:
    owner, project_id, headers, worker, skill_id, v1, v2, task_id = _canary_setup(client, role_urls)
    unrelated_eval = record_evaluation(
        Database(role_urls["aether_verifier"]), owner_id=owner, project_id=project_id, candidate_version_id=v1,
        dataset_sha256="e" * 64, purpose="validation", split="dev", metrics={"families": 1}, synthetic=False,
    )
    other_intent = {"action": "send_email", "to": "someone@example.test"}
    approval = OperationLedger(worker).request_approval(owner_id=owner, project_id=project_id, task_id=task_id, intent=other_intent, risk="R2")
    digest = rows(worker, owner, project_id, "SELECT intent_digest FROM approvals WHERE id = :id", id=approval)[0]["intent_digest"]
    OperationLedger(Database(role_urls["aether_api"])).decide(owner_id=owner, project_id=project_id, task_id=task_id, approval_id=approval, decision="approved", expected_intent_hash=digest)
    promote = client.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/deployments",
        headers={**headers, "Idempotency-Key": key(), "If-Match": "0"},
        json={"action": "promote", "target_version_id": v2, "evaluation_id": unrelated_eval, "expected_previous_version_id": None, "approval_id": approval, "report_sha256": "0" * 64},
    )
    assert promote.status_code == 202, promote.text
    _apply(client, role_urls)
    status = _status(worker, owner, project_id, promote.json()["command_id"])
    canary = rows(worker, owner, project_id, "SELECT version_id::text AS v FROM skill_deployments WHERE skill_id = :s AND slot = 'canary'", s=skill_id)
    detail = f"用一个发邮件的审批、一个评测别的版本且证据不足的评测去晋升 v2：命令={status} canary={canary}"
    assert status == "FAILED", detail
    assert canary == [], detail
