"""SYS-06 and SYS-04. Real PostgreSQL, including RLS.

Uploads are committed before a later transaction reads them. An invisible
uncommitted artifact is not evidence that an untrusted test_run was rejected.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.goal_effect import _ensure_contract, materialize_goal_effect
from tests.integration.goal_approval import record_owner_approval
from aether_worker.memory import (
    MemoryError,
    _is_project_test_run,
    _patch,
    apply_memory_commands,
    assemble_context,
    authorize_declassification,
    remember,
)
from aether_worker.research import record_verification
from tests.integration.system_audit import FACT, endpoints, key, project, scalar, store_for
from tests.integration.test_auth import ORIGIN


def _worker(role_urls: dict[str, str]) -> Database:
    return Database(role_urls["aether_worker"])


def _verifier(role_urls: dict[str, str]) -> Database:
    return Database(role_urls["aether_verifier"])


def _json_artifact(store, owner_id: str, project_id: str, body: dict) -> dict:
    stored = store.put(
        owner_id=owner_id,
        project_id=project_id,
        data=json.dumps(body).encode(),
        media_type="application/json",
    )
    return {"artifact_id": str(stored["artifact_id"]), "sha256": str(stored["sha256"])}


def _task_contract(worker: Database, owner_id: str, project_id: str) -> tuple[str, str, str]:
    digest = "ab" * 32
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_id = conn.execute(
            text(
                "INSERT INTO tasks (project_id, original_goal, workflow_id, status) "
                "VALUES (:project_id, '测试运行', :workflow, 'QUEUED') RETURNING id::text"
            ),
            {"project_id": project_id, "workflow": f"run-{uuid.uuid4()}"},
        ).scalar_one()
        contract_id = conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, '{}'::jsonb, :digest, :owner, now()) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "digest": digest, "owner": owner_id},
        ).scalar_one()
    return str(task_id), str(contract_id), digest


def _attest(role_urls, owner_id: str, project_id: str, artifact: dict, *, criterion_id: str, verdict_pass: bool) -> dict[str, str]:
    task_id, contract_id, digest = _task_contract(_worker(role_urls), owner_id, project_id)
    return record_verification(
        _verifier(role_urls),
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        contract_id=contract_id,
        artifact_id=artifact["artifact_id"],
        report_artifact_id=artifact["artifact_id"],
        criterion_id=criterion_id,
        artifact_sha256=artifact["sha256"],
        observed_sha256=artifact["sha256"],
        contract_sha256=digest,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        now=datetime.now(UTC),
        checks_passed=verdict_pass,
        outcome="pass" if verdict_pass else "fail",
    )


def _seen(worker: Database, store, owner_id: str, project_id: str, artifact_id: str) -> bool:
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return _is_project_test_run(conn, store, owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)


def test_committed_user_json_is_not_a_test_run(client, role_urls) -> None:
    owner_id, project_id, _headers = project(client)
    worker = _worker(role_urls)
    store = store_for(client, role_urls)
    failed = _json_artifact(store, owner_id, project_id, {"kind": "test_run", "status": "failed", "exit_code": 99})
    passed = _json_artifact(store, owner_id, project_id, {"kind": "test_run", "status": "passed", "exit_code": 0, "passed": True})
    assert _seen(worker, store, owner_id, project_id, failed["artifact_id"]) is False
    assert _seen(worker, store, owner_id, project_id, passed["artifact_id"]) is False
    memory_id = remember(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        claim="所有测试都通过",
        kind="semantic",
        category="fact",
        requested_status="observed_test",
        test_run_artifact_id=failed["artifact_id"],
    )
    status = scalar(worker, owner_id, project_id, "SELECT epistemic_status FROM memories WHERE id = :id", id=memory_id)
    assert status == "proposed"


def test_test_run_requires_verifier_ledger_bound_to_the_bytes(client, role_urls) -> None:
    owner_id, project_id, _headers = project(client)
    worker = _worker(role_urls)
    verifier = _verifier(role_urls)
    store = store_for(client, role_urls)
    artifact = _json_artifact(store, owner_id, project_id, {"kind": "test_run", "status": "passed", "exit_code": 0})
    decoy = _attest(role_urls, owner_id, project_id, artifact, criterion_id="goal_artifact", verdict_pass=True)
    assert decoy["verdict"] == "PASS"
    assert _seen(worker, store, owner_id, project_id, artifact["artifact_id"]) is False
    other = _json_artifact(
        store,
        owner_id,
        project_id,
        {"kind": "test_run", "status": "passed", "exit_code": 0, "label": "contradiction"},
    )
    failed = _attest(role_urls, owner_id, project_id, other, criterion_id="test_run", verdict_pass=False)
    assert failed["verdict"] == "FAIL"
    contradicted = _attest(role_urls, owner_id, project_id, other, criterion_id="test_run", verdict_pass=True)
    assert contradicted["verdict"] == "PASS"
    assert _seen(worker, store, owner_id, project_id, other["artifact_id"]) is False
    task_id, contract_id, digest = _task_contract(worker, owner_id, project_id)
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO verification_results (project_id, task_id, contract_id, artifact_id, criterion_id, verdict, "
                "artifact_sha256, contract_sha256, verifier_identity, report_artifact_id, reason, checked_at, observed_sha256) "
                "VALUES (:project_id, :task_id, :contract_id, :artifact_id, 'test_run', 'PASS', :wrong, :digest, "
                "'aether_verifier', :artifact_id, 'hash mismatch', now(), :wrong)"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "contract_id": contract_id,
                "artifact_id": artifact["artifact_id"],
                "wrong": "cd" * 32,
                "digest": digest,
            },
        )
        conn.execute(
            text(
                "INSERT INTO verification_results (project_id, task_id, contract_id, artifact_id, criterion_id, verdict, "
                "artifact_sha256, contract_sha256, verifier_identity, report_artifact_id, reason, checked_at, observed_sha256) "
                "VALUES (:project_id, :task_id, :contract_id, :artifact_id, 'test_run', 'PASS', :sha, :digest, "
                "'aether_api', :artifact_id, 'untrusted issuer', now(), :sha)"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "contract_id": contract_id,
                "artifact_id": artifact["artifact_id"],
                "sha": artifact["sha256"],
                "digest": digest,
            },
        )
    assert _seen(worker, store, owner_id, project_id, artifact["artifact_id"]) is False
    attested = _attest(role_urls, owner_id, project_id, artifact, criterion_id="test_run", verdict_pass=True)
    assert attested["verdict"] == "PASS"
    assert attested["verifier"] == "aether_verifier"
    assert _seen(worker, store, owner_id, project_id, artifact["artifact_id"]) is True
    memory_id = remember(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        claim="账本绑定的测试通过了",
        kind="semantic",
        category="fact",
        requested_status="observed_test",
        test_run_artifact_id=artifact["artifact_id"],
    )
    status = scalar(worker, owner_id, project_id, "SELECT epistemic_status FROM memories WHERE id = :id", id=memory_id)
    assert status == "observed_test"


def test_failed_bytes_stay_unverified_even_with_a_pass_row(client, role_urls) -> None:
    owner_id, project_id, _headers = project(client)
    worker = _worker(role_urls)
    store = store_for(client, role_urls)
    failed = _json_artifact(store, owner_id, project_id, {"kind": "test_run", "status": "failed", "exit_code": 99})
    attested = _attest(role_urls, owner_id, project_id, failed, criterion_id="test_run", verdict_pass=True)
    assert attested["verdict"] == "PASS"
    assert _seen(worker, store, owner_id, project_id, failed["artifact_id"]) is False


def test_confirm_keeps_sensitivity_sources_and_tombstone(client, role_urls) -> None:
    owner_id, project_id, headers = project(client)
    worker = _worker(role_urls)
    store = store_for(client, role_urls)
    source = store.put(owner_id=owner_id, project_id=project_id, data="来源正文".encode(), media_type="text/plain")
    claim = "内部发布日：星期三。"
    memory_id = remember(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        claim=claim,
        kind="semantic",
        category="fact",
        source_artifact_ids=[str(source["artifact_id"])],
        sensitivity="high",
    )
    claim_artifact = scalar(
        worker, owner_id, project_id, "SELECT claim_artifact_id::text FROM memories WHERE id = :id", id=memory_id
    )
    listed = client.get(f"/v1/projects/{project_id}/memories").json()["items"]
    current = next(item for item in listed if item["id"] == memory_id)
    confirmed = client.patch(
        f"/v1/projects/{project_id}/memories/{memory_id}",
        headers={**headers, "Idempotency-Key": key(), "If-Match": str(current["revision"])},
        json={"action": "confirm"},
    )
    assert confirmed.status_code == 202, confirmed.text
    apply_memory_commands(worker, store, owner_id=owner_id, project_id=project_id)
    again = client.get(f"/v1/projects/{project_id}/memories").json()["items"]
    successor = next(item for item in again if item["epistemic_status"] == "user_confirmed")
    assert successor["claim"] == claim
    assert successor["provenance"]["sensitivity"] == "high"
    assert successor["provenance"]["source_artifact_ids"] == [str(source["artifact_id"])]
    assembled = assemble_context(
        project_id=project_id,
        goal="查看内部计划",
        plan="",
        evidence=[],
        memories=[{**successor, "project_id": project_id}],
        skills=[],
        history="",
        materials=[],
        budget_tokens=500,
        destination={"approved": False},
    )
    assert not any(part["name"] == "memory" for part in assembled["parts"])
    assert any(part["reason"] == "export_denied" for part in assembled["dropped"])
    deleted = scalar(worker, owner_id, project_id, "SELECT deleted_at FROM artifacts WHERE id = :id", id=claim_artifact)
    classification = scalar(
        worker,
        owner_id,
        project_id,
        "SELECT classification FROM artifacts WHERE id = (SELECT claim_artifact_id FROM memories WHERE id = :id)",
        id=successor["id"],
    )
    assert deleted is not None
    assert classification == "memory_claim"


def test_confirm_payload_cannot_lower_sensitivity_or_drop_sources(client, role_urls) -> None:
    owner_id, project_id, _headers = project(client)
    worker = _worker(role_urls)
    store = store_for(client, role_urls)
    source = store.put(owner_id=owner_id, project_id=project_id, data="来源正文".encode(), media_type="text/plain")
    memory_id = remember(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        claim="内部窗口：星期五。",
        kind="semantic",
        category="fact",
        source_artifact_ids=[str(source["artifact_id"])],
        sensitivity="high",
    )
    revised = str(uuid.uuid4())
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        _patch(
            conn,
            store,
            owner_id=owner_id,
            project_id=project_id,
            payload={
                "memory_id": memory_id,
                "result_id": revised,
                "command_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "action": "confirm",
                "sensitivity": "standard",
                "source_artifact_ids": [],
            },
        )
    provenance = scalar(worker, owner_id, project_id, "SELECT provenance FROM memories WHERE id = :id", id=revised)
    body = provenance if isinstance(provenance, dict) else json.loads(provenance)
    assert body["sensitivity"] == "high"
    assert body["source_artifact_ids"] == [str(source["artifact_id"])]


def test_revision_inherits_sensitivity_until_a_separate_grant(client, role_urls) -> None:
    owner_id, project_id, headers = project(client)
    worker = _worker(role_urls)
    store = store_for(client, role_urls)
    source = store.put(owner_id=owner_id, project_id=project_id, data="来源".encode(), media_type="text/plain")
    memory_id = remember(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        claim="内部代号：甲。",
        kind="semantic",
        category="fact",
        source_artifact_ids=[str(source["artifact_id"])],
        sensitivity="high",
    )
    unused = authorize_declassification(
        worker,
        owner_id=owner_id,
        project_id=project_id,
        memory_id=memory_id,
        from_sensitivity="high",
        to_sensitivity="standard",
    )
    corrected = client.patch(
        f"/v1/projects/{project_id}/memories/{memory_id}",
        headers={**headers, "Idempotency-Key": key(), "If-Match": "1"},
        json={"action": "correct", "claim": "内部代号：乙。"},
    )
    assert corrected.status_code == 202, corrected.text
    apply_memory_commands(worker, store, owner_id=owner_id, project_id=project_id)
    rows = client.get(f"/v1/projects/{project_id}/memories").json()["items"]
    active = next(item for item in rows if item["epistemic_status"] != "superseded")
    assert active["provenance"]["sensitivity"] == "high"
    assert active["provenance"]["source_artifact_ids"] == [str(source["artifact_id"])]
    still_open = scalar(
        worker, owner_id, project_id, "SELECT revoked_at FROM capability_grants WHERE id = :id", id=unused
    )
    assert still_open is None
    with pytest.raises(MemoryError) as wrong_memory:
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            _patch(
                conn,
                store,
                owner_id=owner_id,
                project_id=project_id,
                payload={
                    "memory_id": active["id"],
                    "result_id": str(uuid.uuid4()),
                    "command_id": str(uuid.uuid4()),
                    "expected_revision": active["revision"],
                    "action": "declassify",
                    "sensitivity": "standard",
                    "authorization_id": unused,
                },
            )
    assert wrong_memory.value.code == "declassification_unauthorized"
    grant = authorize_declassification(
        worker,
        owner_id=owner_id,
        project_id=project_id,
        memory_id=active["id"],
        from_sensitivity="high",
        to_sensitivity="standard",
    )
    with pytest.raises(MemoryError) as denied:
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            _patch(
                conn,
                store,
                owner_id=owner_id,
                project_id=project_id,
                payload={
                    "memory_id": active["id"],
                    "result_id": str(uuid.uuid4()),
                    "command_id": str(uuid.uuid4()),
                    "expected_revision": active["revision"],
                    "action": "declassify",
                    "sensitivity": "standard",
                },
            )
    assert denied.value.code == "declassification_unauthorized"
    lowered = client.patch(
        f"/v1/projects/{project_id}/memories/{active['id']}",
        headers={**headers, "Idempotency-Key": key(), "If-Match": str(active["revision"])},
        json={"action": "declassify", "sensitivity": "standard", "authorization_id": grant},
    )
    assert lowered.status_code == 202, lowered.text
    apply_memory_commands(worker, store, owner_id=owner_id, project_id=project_id)
    final = client.get(f"/v1/projects/{project_id}/memories").json()["items"]
    released = next(item for item in final if item["epistemic_status"] != "superseded" and item["revision"] == active["revision"] + 1)
    assert released["provenance"]["sensitivity"] == "standard"
    assert released["provenance"]["source_artifact_ids"] == [str(source["artifact_id"])]
    assert released["epistemic_status"] == "proposed"
    consumed = scalar(worker, owner_id, project_id, "SELECT revoked_at FROM capability_grants WHERE id = :id", id=grant)
    assert consumed is not None
    with pytest.raises(MemoryError) as still_high:
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            _patch(
                conn,
                store,
                owner_id=owner_id,
                project_id=project_id,
                payload={
                    "memory_id": active["id"],
                    "result_id": str(uuid.uuid4()),
                    "command_id": str(uuid.uuid4()),
                    "expected_revision": active["revision"],
                    "action": "declassify",
                    "sensitivity": "standard",
                    "authorization_id": grant,
                },
            )
    assert still_high.value.code == "declassification_unauthorized"
    with pytest.raises(MemoryError) as reused:
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            _patch(
                conn,
                store,
                owner_id=owner_id,
                project_id=project_id,
                payload={
                    "memory_id": released["id"],
                    "result_id": str(uuid.uuid4()),
                    "command_id": str(uuid.uuid4()),
                    "expected_revision": released["revision"],
                    "action": "declassify",
                    "sensitivity": "standard",
                    "authorization_id": grant,
                },
            )
    assert reused.value.code == "declassification_rejected"


def _draft(project_id: str, task_id: str, goal: str, criteria: list[dict]) -> dict:
    return {
        "project_id": project_id,
        "task_id": task_id,
        "contract_version": 1,
        "goal": goal,
        "required_artifacts": ["answer.txt"],
        "criteria": criteria,
        "created_at": "2026-10-16T00:00:00Z",
    }


def _put_draft(client, project_id: str, task_id: str, headers: dict, draft: dict) -> None:
    snapshot = client.get(f"/v1/projects/{project_id}/tasks/{task_id}").json()
    revised = client.put(
        f"/v1/projects/{project_id}/tasks/{task_id}/contract",
        headers={**headers, "If-Match": str(snapshot["version"]), "Idempotency-Key": key()},
        json=draft,
    )
    assert revised.status_code == 200, revised.text


def _start(client, role_urls, owner_id: str, project_id: str, headers: dict, goal: str) -> dict:
    created = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**headers, "Idempotency-Key": key()},
        json={"goal": goal},
    )
    assert created.status_code == 202, created.text
    body = created.json()
    payload = {"project_id": project_id, "task_id": body["resource_id"], "command_id": body["command_id"]}
    record_owner_approval(Database(role_urls["aether_worker"]), owner_id=owner_id, project_id=project_id, task_id=payload["task_id"])
    return payload


def _contract_content(worker: Database, owner_id: str, project_id: str, task_id: str):
    raw = scalar(
        worker,
        owner_id,
        project_id,
        "SELECT content FROM goal_contracts WHERE task_id = :task_id ORDER BY contract_version DESC LIMIT 1",
        task_id=task_id,
    )
    return raw if isinstance(raw, dict) else json.loads(raw)


def test_approved_draft_is_frozen_and_not_replaced(client, role_urls) -> None:
    owner_id, project_id, headers = project(client)
    worker = _worker(role_urls)
    store = store_for(client, role_urls)
    store.put(owner_id=owner_id, project_id=project_id, data=f"{FACT}\n".encode(), media_type="text/plain")
    goal = "请根据已上传资料回答沸点"
    strict = _start(client, role_urls, owner_id, project_id, headers, goal)
    review = {
        "criterion_id": "owner_review",
        "template": "human_review",
        "description": "所有者确认沸点摘录后才能算完成",
        "mandatory": True,
        "parameters": {"rubric": "核对一百摄氏度", "reviewer_role": "owner"},
    }
    _put_draft(client, project_id, strict["task_id"], headers, _draft(project_id, strict["task_id"], goal, [review]))
    result = materialize_goal_effect(endpoints(role_urls, store.root, provider="scripted"), strict)
    frozen = _contract_content(worker, owner_id, project_id, strict["task_id"])
    assert frozen["criteria"] == [review]
    assert frozen["required_artifacts"] == ["answer.txt"]
    assert frozen != {"criterion_id": "goal_artifact", "goal": goal}
    assert result["status"] == "NEEDS_REVIEW"
    verdict = scalar(
        _verifier(role_urls),
        owner_id,
        project_id,
        "SELECT verdict FROM verification_results WHERE task_id = :task_id",
        task_id=strict["task_id"],
    )
    assert verdict == "UNVERIFIABLE"

    sized = _start(client, role_urls, owner_id, project_id, headers, goal)
    exists = {
        "criterion_id": "answer_bytes",
        "template": "artifact_exists",
        "description": "回答至少有一个字节",
        "mandatory": True,
        "parameters": {"logical_name": "answer.txt", "minimum_bytes": 1},
    }
    _put_draft(client, project_id, sized["task_id"], headers, _draft(project_id, sized["task_id"], goal, [exists]))
    sized_result = materialize_goal_effect(endpoints(role_urls, store.root, provider="scripted"), sized)
    sized_contract = _contract_content(worker, owner_id, project_id, sized["task_id"])
    assert sized_contract["criteria"] == [exists]
    assert sized_result["status"] == "SUCCEEDED"

    plain = _start(client, role_urls, owner_id, project_id, headers, goal)
    plain_result = materialize_goal_effect(endpoints(role_urls, store.root, provider="scripted"), plain)
    assert plain_result["status"] == "SUCCEEDED"
    assert _contract_content(worker, owner_id, project_id, plain["task_id"]) == {"criterion_id": "goal_artifact", "goal": goal}


def test_draft_mismatch_does_not_fall_back_to_the_automatic_contract(client, role_urls) -> None:
    owner_id, project_id, headers = project(client)
    worker = _worker(role_urls)
    goal = "请根据已上传资料回答沸点"
    started = _start(client, role_urls, owner_id, project_id, headers, goal)
    weaker = {
        "criterion_id": "goal_artifact",
        "template": "artifact_exists",
        "description": "只要有文件",
        "parameters": {"logical_name": "answer.txt", "minimum_bytes": 1},
    }
    draft = _draft(project_id, started["task_id"], "另一份更弱的目标", [weaker])
    _put_draft(client, project_id, started["task_id"], headers, draft)
    with pytest.raises(RuntimeError, match="draft goal"):
        _ensure_contract(worker, owner_id=owner_id, project_id=project_id, task_id=started["task_id"], goal=goal, approved_by=owner_id)
    stored = scalar(worker, owner_id, project_id, "SELECT count(*) FROM goal_contracts WHERE task_id = :task_id", task_id=started["task_id"])
    assert stored == 0


def test_later_draft_does_not_rewrite_a_frozen_contract(client, role_urls) -> None:
    owner_id, project_id, headers = project(client)
    worker = _worker(role_urls)
    goal = "保持已冻结的条款"
    started = _start(client, role_urls, owner_id, project_id, headers, goal)
    original = {
        "criterion_id": "keep",
        "template": "human_review",
        "description": "原始强制人工条款",
        "mandatory": True,
        "parameters": {"rubric": "不能被事后草案换掉"},
    }
    _put_draft(client, project_id, started["task_id"], headers, _draft(project_id, started["task_id"], goal, [original]))
    contract_id, digest = _ensure_contract(worker, owner_id=owner_id, project_id=project_id, task_id=started["task_id"], goal=goal, approved_by=owner_id)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE tasks SET draft_contract = CAST(:draft AS jsonb) WHERE project_id = :project_id AND id = :task_id"),
            {
                "draft": json.dumps(_draft(project_id, started["task_id"], goal, [{
                    "criterion_id": "weaker",
                    "template": "artifact_exists",
                    "description": "事后换成更弱的文件存在检查",
                    "parameters": {"logical_name": "answer.txt", "minimum_bytes": 1},
                }])),
                "project_id": project_id,
                "task_id": started["task_id"],
            },
        )
    again_id, again_digest = _ensure_contract(worker, owner_id=owner_id, project_id=project_id, task_id=started["task_id"], goal=goal)
    assert (again_id, again_digest) == (contract_id, digest)
    assert _contract_content(worker, owner_id, project_id, started["task_id"])["criteria"] == [original]


def test_api_rejects_declassify_without_an_authorization_header(client, role_urls) -> None:
    owner_id, project_id, headers = project(client)
    worker = _worker(role_urls)
    store = store_for(client, role_urls)
    memory_id = remember(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        claim="内部代号：丙。",
        kind="semantic",
        category="fact",
        sensitivity="high",
    )
    rejected = client.patch(
        f"/v1/projects/{project_id}/memories/{memory_id}",
        headers={**headers, "Idempotency-Key": key(), "If-Match": "1", "Origin": ORIGIN["Origin"]},
        json={"action": "declassify", "sensitivity": "standard"},
    )
    assert rejected.status_code == 422, rejected.text
