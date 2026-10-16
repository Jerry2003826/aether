"""P0-01: memory claims are not research sources or verifier evidence."""

from __future__ import annotations

from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from aether_worker.memory import apply_memory_commands, remember
from tests.integration.goal_approval import record_owner_approval
from tests.integration.system_audit import endpoints, fake_transport, key, project, rows, scalar, store_for


def _memory(client, role_urls, owner: str, project_id: str, headers: dict[str, str], claim: str) -> dict:
    created = client.post(
        f"/v1/projects/{project_id}/memories",
        headers={**headers, "Idempotency-Key": key()},
        json={"claim": claim, "kind": "semantic", "category": "fact"},
    )
    assert created.status_code == 202, created.text
    apply_memory_commands(Database(role_urls["aether_worker"]), store_for(client, role_urls), owner_id=owner, project_id=project_id)
    listed = client.get(f"/v1/projects/{project_id}/memories").json()["items"]
    return next(item for item in listed if item["claim"] == claim)


def _task(client, role_urls, owner: str, project_id: str, headers: dict[str, str], goal: str, **extra) -> dict:
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": goal, **extra})
    assert created.status_code == 202, created.text
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    record_owner_approval(Database(role_urls["aether_worker"]), owner_id=owner, project_id=project_id, task_id=payload["task_id"])
    return payload


def test_deleted_memory_claim_never_reaches_the_model_or_the_verifier(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    claim = "乙服务的请求超时时间是30秒。"
    memory = _memory(client, role_urls, owner, project_id, headers, claim)
    deleted = client.delete(
        f"/v1/projects/{project_id}/memories/{memory['id']}",
        headers={**headers, "Idempotency-Key": key(), "If-Match": str(memory["revision"])},
    )
    assert deleted.status_code == 202, deleted.text
    apply_memory_commands(Database(role_urls["aether_worker"]), store_for(client, role_urls), owner_id=owner, project_id=project_id)
    assert client.get(f"/v1/projects/{project_id}/memories").json()["items"] == []

    state = fake_transport(monkeypatch, output=claim)
    payload = _task(client, role_urls, owner, project_id, headers, "根据资料，乙服务的请求超时时间是多少秒？")
    result = materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    verifications = rows(
        Database(role_urls["aether_verifier"]),
        owner,
        project_id,
        "SELECT verdict, reason FROM verification_results WHERE task_id = :t",
        t=payload["task_id"],
    )
    leaked = [prompt for prompt in state["prompts"] if claim in prompt]
    detail = f"status={result.get('status')} calls={state['calls']} prompt_has_deleted_claim={bool(leaked)} verifications={verifications}"
    assert not leaked, detail
    assert result["status"] != "SUCCEEDED", detail


def test_corrected_memory_old_claim_is_not_ground_truth(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    wrong = "丙接口每分钟最多接受90次请求。"
    right = "丙接口每分钟最多接受60次请求。"
    memory = _memory(client, role_urls, owner, project_id, headers, wrong)
    corrected = client.patch(
        f"/v1/projects/{project_id}/memories/{memory['id']}",
        headers={**headers, "Idempotency-Key": key(), "If-Match": str(memory["revision"])},
        json={"action": "correct", "claim": right},
    )
    assert corrected.status_code == 202, corrected.text
    apply_memory_commands(Database(role_urls["aether_worker"]), store_for(client, role_urls), owner_id=owner, project_id=project_id)
    visible = [item["claim"] for item in client.get(f"/v1/projects/{project_id}/memories").json()["items"] if item["epistemic_status"] != "superseded"]
    assert visible == [right]

    state = fake_transport(monkeypatch, output=wrong)
    payload = _task(client, role_urls, owner, project_id, headers, "根据资料，丙接口每分钟最多接受多少次请求？")
    result = materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    leaked = [prompt for prompt in state["prompts"] if wrong in prompt]
    detail = f"status={result.get('status')} prompt_has_superseded_claim={bool(leaked)}"
    assert not leaked, detail
    assert result["status"] != "SUCCEEDED", detail


def test_high_sensitivity_memory_is_not_sent_to_the_cloud_model(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("note.txt", "丁服务每天凌晨两点备份一次。\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    private = "丁服务的值班电话是13800000000。"
    remember(
        Database(role_urls["aether_worker"]),
        store_for(client, role_urls),
        owner_id=owner,
        project_id=project_id,
        claim=private,
        kind="semantic",
        category="fact",
        sensitivity="high",
    )
    classification = scalar(
        Database(role_urls["aether_worker"]),
        owner,
        project_id,
        "SELECT a.classification FROM artifacts AS a JOIN memories AS m ON m.claim_artifact_id = a.id WHERE m.claim = :c",
        c=private,
    )
    assert classification == "memory_claim"
    state = fake_transport(monkeypatch, output="资料记载：丁服务每天凌晨两点备份一次。")
    payload = _task(client, role_urls, owner, project_id, headers, "根据资料，丁服务什么时候备份？")
    materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    leaked = [prompt for prompt in state["prompts"] if private in prompt]
    assert not leaked, f"高敏记忆进入了发往云端模型的提示词：calls={state['calls']}"


def test_proposed_memory_is_not_verification_evidence(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    claim = "庚服务的配额是每分钟40次。"
    memory = _memory(client, role_urls, owner, project_id, headers, claim)
    assert memory["epistemic_status"] == "proposed"
    state = fake_transport(monkeypatch, output=claim)
    payload = _task(client, role_urls, owner, project_id, headers, "根据资料，庚服务每分钟的配额是多少次？")
    result = materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    leaked = [prompt for prompt in state["prompts"] if claim in prompt]
    detail = f"status={result.get('status')} leaked={bool(leaked)}"
    assert not leaked, detail
    assert result["status"] != "SUCCEEDED", detail


def test_explicit_memory_claim_is_not_an_allowed_source(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    claim = "辛服务的超时是15秒。"
    memory = _memory(client, role_urls, owner, project_id, headers, claim)
    claim_id = scalar(
        Database(role_urls["aether_worker"]),
        owner,
        project_id,
        "SELECT claim_artifact_id::text FROM memories WHERE id = :id",
        id=memory["id"],
    )
    state = fake_transport(monkeypatch, output=claim)
    payload = _task(client, role_urls, owner, project_id, headers, "根据资料，辛服务的超时是多少秒？", allowed_source_ids=[claim_id])
    result = materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    leaked = [prompt for prompt in state["prompts"] if claim in prompt]
    detail = f"status={result.get('status')} stop={result.get('stop_reason')} leaked={bool(leaked)}"
    assert not leaked, detail
    assert result["status"] != "SUCCEEDED", detail
    assert result.get("stop_reason") == "allowed_sources_invalid", detail


def test_legacy_claim_rows_are_reclassified_and_retired(client, role_urls) -> None:
    owner, project_id, _headers = project(client)
    worker = Database(role_urls["aether_worker"])
    store = store_for(client, role_urls)
    stored = store.put(owner_id=owner, project_id=project_id, data="旧记忆仍被标成资料。".encode(), media_type="text/plain")
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO memories (project_id, memory_key, revision, kind, claim, category, claim_artifact_id, provenance, epistemic_status) "
                "VALUES (:project_id, :memory_key, 1, 'semantic', :claim, 'fact', :artifact_id, '{\"sensitivity\":\"standard\"}'::jsonb, 'superseded')"
            ),
            {
                "project_id": project_id,
                "memory_key": stored["artifact_id"],
                "claim": "旧记忆仍被标成资料。",
                "artifact_id": stored["artifact_id"],
            },
        )
        conn.execute(
            text(
                "UPDATE artifacts AS artifact SET classification = 'memory_claim' "
                "FROM memories AS memory "
                "WHERE artifact.project_id = memory.project_id AND artifact.id = memory.claim_artifact_id "
                "AND artifact.classification = 'project_private'"
            )
        )
        conn.execute(
            text(
                "UPDATE artifacts AS artifact SET deleted_at = COALESCE(artifact.deleted_at, now()) "
                "FROM memories AS memory "
                "WHERE artifact.project_id = memory.project_id AND artifact.id = memory.claim_artifact_id "
                "AND (memory.deleted_at IS NOT NULL OR memory.epistemic_status = 'superseded') "
                "AND artifact.deleted_at IS NULL"
            )
        )
    classification = scalar(
        worker,
        owner,
        project_id,
        "SELECT classification FROM artifacts WHERE id = :id",
        id=stored["artifact_id"],
    )
    deleted_at = scalar(worker, owner, project_id, "SELECT deleted_at FROM artifacts WHERE id = :id", id=stored["artifact_id"])
    assert classification == "memory_claim"
    assert deleted_at is not None
