"""P0-01 · 记忆的 claim 被存成 project_private 文本产物，于是成了研究资料和验收依据。

不变量 INV-DEL-01：删除或更正后的记忆不能再进入任何新任务的模型上下文；
不变量 INV-TRUTH-03：未确认的记忆不能充当验收器的"资料"去证明答案成立；
不变量 INV-EGRESS-01：高敏记忆不能未经批准送到云端模型。
"""

from __future__ import annotations

from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from aether_worker.memory import apply_memory_commands, remember
from audit_helpers import endpoints, fake_transport, key, project, rows, scalar, store_for


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


def _task(client, project_id: str, headers: dict[str, str], goal: str, **extra) -> dict:
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": goal, **extra})
    assert created.status_code == 202, created.text
    return {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}


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
    payload = _task(client, project_id, headers, "根据资料，乙服务的请求超时时间是多少秒？")
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
    payload = _task(client, project_id, headers, "根据资料，丙接口每分钟最多接受多少次请求？")
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
    assert scalar(Database(role_urls["aether_worker"]), owner, project_id, "SELECT count(*) FROM memories WHERE claim = :c", c=private) == 1
    state = fake_transport(monkeypatch, output="资料记载：丁服务每天凌晨两点备份一次。")
    payload = _task(client, project_id, headers, "根据资料，丁服务什么时候备份？")
    materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    leaked = [prompt for prompt in state["prompts"] if private in prompt]
    assert not leaked, f"高敏记忆进入了发往云端模型的提示词：calls={state['calls']}"
