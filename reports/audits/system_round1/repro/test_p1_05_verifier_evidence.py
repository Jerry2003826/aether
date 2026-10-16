"""P1-05 · 验收器的证据基础不对：不绑定生成时的快照，也不识别互相矛盾的资料。

不变量 INV-TRUTH-01：验收绑定生成时的来源快照；资料在生成后变化只能让验收不可判定或重做，
不能用模型没看过的资料去判定成功或失败；
不变量 INV-TRUTH-03：对明显矛盾的证据只能给出 UNVERIFIABLE / 人工复核，不能凑齐引用就验收（docs/14 D 节）。
"""

from __future__ import annotations

import aether_worker.goal_effect as ge
from aether_storage.db import Database
from audit_helpers import endpoints, fake_transport, key, project, rows, store_for

A = "甲接口每分钟最多接受60次请求。"
B = "甲接口每分钟最多接受90次请求。"
GOAL = "根据资料，甲接口每分钟最多接受多少次请求？"


def test_verification_uses_the_snapshot_the_model_saw(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", A.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    state = fake_transport(monkeypatch, output=f"资料记载：{A}")
    original = ge._ensure_verification

    def upload_between_answer_and_verification(*args, **kwargs):
        late = client.post(
            f"/v1/projects/{project_id}/artifacts",
            headers={**headers, "Idempotency-Key": key()},
            files={"file": ("later.txt", B.encode(), "text/plain")},
        )
        assert late.status_code == 201, late.text
        return original(*args, **kwargs)

    monkeypatch.setattr(ge, "_ensure_verification", upload_between_answer_and_verification)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": GOAL})
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    result = ge.materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    sent = rows(Database(role_urls["aether_worker"]), owner, project_id, "SELECT usage_detail->>'source_snapshot_sha256' AS s FROM model_requests WHERE task_id = :t", t=payload["task_id"])[0]["s"]
    checked = rows(
        Database(role_urls["aether_verifier"]),
        owner,
        project_id,
        "SELECT source_snapshot_sha256 AS s, verdict, reason FROM verification_results WHERE task_id = :t",
        t=payload["task_id"],
    )[0]
    detail = (
        f"模型只看到一份资料并正确作答（调用 {state['calls']} 次）；验收用的快照 {checked['s'][:12]} ≠ 发送时 {sent[:12]}；"
        f"判定 {checked['verdict']}（{checked['reason']}），任务 {result['status']}"
    )
    assert checked["s"] == sent, detail


def test_contradictory_sources_are_not_verified_as_success(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    ids = []
    for name, body in (("a.txt", A), ("b.txt", B)):
        uploaded = client.post(
            f"/v1/projects/{project_id}/artifacts",
            headers={**headers, "Idempotency-Key": key()},
            files={"file": (name, body.encode(), "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        ids.append(uploaded.json()["artifact_id"])
    fake_transport(monkeypatch, output=A + B)
    created = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**headers, "Idempotency-Key": key()},
        json={"goal": GOAL, "allowed_source_ids": ids},
    )
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    result = ge.materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    verdict = rows(Database(role_urls["aether_verifier"]), owner, project_id, "SELECT verdict, reason FROM verification_results WHERE task_id = :t", t=payload["task_id"])
    detail = f"两份资料一个说 60 一个说 90，答案两个都写：任务 {result['status']}，验收 {verdict}"
    assert result["status"] != "SUCCEEDED", detail
