"""P2-02, P2-03, and the terminal-status half of P2-06."""

from __future__ import annotations

import json
import threading
import time

from sqlalchemy import text

from aether_api.control import _append_event
from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from tests.integration.system_audit import endpoints, key, project, scalar, store_for


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
    events = [json.loads(line[len("data: ") :]) for line in stream.text.splitlines() if line.startswith("data: ") and '"event_type"' in line]
    kinds = [event["event_type"] for event in events]
    statuses = [event["payload"].get("status") for event in events]
    detail = f"任务已 SUCCEEDED，事件流里只有 {kinds}，payload.status={statuses}"
    assert "SUCCEEDED" in statuses or any("succeeded" in kind for kind in kinds), detail


def test_api_command_event_and_worker_event_do_not_collide(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "并发事件"})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    api = Database(role_urls["aether_api"])
    errors: list[str] = []

    def api_side() -> None:
        try:
            with api.transaction(owner_id=owner, project_id=project_id) as conn:
                _append_event(conn, project_id=project_id, task_id=task_id, event_type="command.accepted", payload={"kind": "pause"})
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")

    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        sequence = conn.execute(
            text("SELECT coalesce(max(sequence), 0) + 1 FROM task_events WHERE project_id = :p AND task_id = :t"),
            {"p": project_id, "t": task_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO task_events (project_id, task_id, sequence, event_key, event_type, payload) "
                "VALUES (:p, :t, :s, :k, 'task.running', CAST(:payload AS jsonb))"
            ),
            {"p": project_id, "t": task_id, "s": sequence, "k": f"running-{task_id}", "payload": json.dumps({"status": "RUNNING"})},
        )
        thread = threading.Thread(target=api_side)
        thread.start()
        time.sleep(0.8)
    thread.join(timeout=10)
    assert not errors, f"worker 事件提交后，同时进行的 API 命令事件失败：{errors}"


def test_database_refuses_to_rewrite_a_cancelled_task_as_succeeded(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "终态守卫"})
    task_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        conn.execute(text("UPDATE tasks SET status = 'CANCELLED' WHERE id = :id"), {"id": task_id})
    error = None
    try:
        with worker.transaction(owner_id=owner, project_id=project_id) as conn:
            conn.execute(text("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = :id"), {"id": task_id})
    except Exception as exc:
        error = type(exc).__name__
    final = None
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        final = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    assert error is not None, f"aether_worker 角色把 CANCELLED 改成了 {final}，数据库没有拦"
    assert final == "CANCELLED"
