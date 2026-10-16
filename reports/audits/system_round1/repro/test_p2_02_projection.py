"""P2-02 · 公开任务视图里的 contract_sha256 来自客户端可写的 draft_contract，终态任务也能改。

不变量 INV-TRUTH-04：对外展示的合同哈希就是验收所绑定的权威合同；批准后的合同不可被改写；
不变量 INV-PROJ-02：任务的每次状态变化都写成可回放的事件，SSE 重连能看到终态。
"""

from __future__ import annotations

import json

from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from audit_helpers import endpoints, key, project, scalar, store_for


def test_public_contract_hash_matches_the_authoritative_contract(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", "事实：水在标准大气压下于一百摄氏度沸腾。\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "请根据已上传资料回答沸点"})
    task_id = created.json()["resource_id"]
    payload = {"project_id": project_id, "task_id": task_id, "command_id": created.json()["command_id"]}
    assert materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root, provider="scripted"), payload)["status"] == "SUCCEEDED"
    before = client.get(f"/v1/projects/{project_id}/tasks/{task_id}").json()
    forged = "f" * 64
    revised = client.put(
        f"/v1/projects/{project_id}/tasks/{task_id}/contract",
        headers={**headers, "If-Match": str(before["version"])},
        json={
            "project_id": project_id,
            "task_id": task_id,
            "goal": "任意改写的目标",
            "required_artifacts": ["answer.txt"],
            "criteria": [{"criterion_id": "x", "template": "human_review", "description": "改写"}],
            "created_at": "2026-10-16T00:00:00Z",
            "contract_version": 1,
            "contract_sha256": forged,
        },
    )
    after = client.get(f"/v1/projects/{project_id}/tasks/{task_id}").json()
    authoritative = scalar(
        Database(role_urls["aether_worker"]),
        owner,
        project_id,
        "SELECT g.contract_sha256 FROM goal_contracts g JOIN tasks t ON t.current_contract_id = g.id WHERE t.id = :t",
        t=task_id,
    )
    verified = {item["contract_sha256"] for item in after["verification_results"]}
    detail = (
        f"任务 {after['status']}；PUT /contract → {revised.status_code}；公开视图 contract_sha256={after['contract_sha256']}；"
        f"权威合同={authoritative}；验收绑定={verified}"
    )
    assert revised.status_code in {409, 422}, detail
    assert after["contract_sha256"] == authoritative, detail


def test_terminal_status_is_visible_in_the_event_stream(client, role_urls) -> None:
    _owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", "事实：水在标准大气压下于一百摄氏度沸腾。\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "请根据已上传资料回答沸点"})
    task_id = created.json()["resource_id"]
    payload = {"project_id": project_id, "task_id": task_id, "command_id": created.json()["command_id"]}
    assert materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root, provider="scripted"), payload)["status"] == "SUCCEEDED"
    stream = client.get(f"/v1/projects/{project_id}/tasks/{task_id}/events")
    assert stream.status_code == 200, stream.text
    events = [json.loads(line[len("data: "):]) for line in stream.text.splitlines() if line.startswith("data: ") and '"event_type"' in line]
    kinds = [event["event_type"] for event in events]
    statuses = [event["payload"].get("status") for event in events]
    detail = f"任务已 SUCCEEDED，事件流里只有 {kinds}，payload.status={statuses}"
    assert "SUCCEEDED" in statuses or any("succeeded" in kind for kind in kinds), detail
