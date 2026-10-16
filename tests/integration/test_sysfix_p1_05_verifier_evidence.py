"""P1-05: verification binds the sources the model saw, and contradictory sources are not a PASS.

INV-TRUTH-01 and INV-TRUTH-03. Ported from reports/audits/system_round1/repro/test_p1_05_verifier_evidence.py.
"""

from __future__ import annotations

import pytest

import aether_worker.goal_effect as ge
from aether_storage.db import Database
from tests.integration.goal_approval import record_owner_approval
from tests.integration.sysfix_b_helpers import endpoints, fake_transport, key, project, rows, store_for

A = "甲接口每分钟最多接受60次请求。"
B = "甲接口每分钟最多接受90次请求。"
GOAL = "根据资料，甲接口每分钟最多接受多少次请求？"
VERIFICATION_SQL = "SELECT source_snapshot_sha256 AS s, verdict, reason FROM verification_results WHERE task_id = :t ORDER BY created_at"


def _upload(client, project_id: str, headers: dict[str, str], name: str, body: str) -> str:
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": (name, body.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return str(uploaded.json()["artifact_id"])


def _start(client, role_urls, owner: str, project_id: str, headers: dict[str, str], body: dict) -> dict[str, str]:
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json=body)
    assert created.status_code == 202, created.text
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    record_owner_approval(Database(role_urls["aether_worker"]), owner_id=owner, project_id=project_id, task_id=payload["task_id"])
    return payload


def test_verification_uses_the_snapshot_the_model_saw(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    _upload(client, project_id, headers, "fact.txt", A)
    state = fake_transport(monkeypatch, output=f"资料记载：{A}")
    original = ge._ensure_verification

    def upload_between_answer_and_verification(*args, **kwargs):
        _upload(client, project_id, headers, "later.txt", B)
        return original(*args, **kwargs)

    monkeypatch.setattr(ge, "_ensure_verification", upload_between_answer_and_verification)
    payload = _start(client, role_urls, owner, project_id, headers, {"goal": GOAL})
    result = ge.materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    sent = rows(Database(role_urls["aether_worker"]), owner, project_id, "SELECT usage_detail->>'source_snapshot_sha256' AS s FROM model_requests WHERE task_id = :t", t=payload["task_id"])[0]["s"]
    checked = rows(Database(role_urls["aether_verifier"]), owner, project_id, VERIFICATION_SQL, t=payload["task_id"])[0]
    detail = (
        f"模型只看到一份资料并正确作答（调用 {state['calls']} 次）；验收用的快照 {checked['s'][:12]} ≠ 发送时 {sent[:12]}；"
        f"判定 {checked['verdict']}（{checked['reason']}），任务 {result['status']}"
    )
    assert checked["s"] == sent, detail


def test_contradictory_sources_are_not_verified_as_success(client, role_urls, monkeypatch) -> None:
    owner, project_id, headers = project(client)
    ids = [_upload(client, project_id, headers, name, body) for name, body in (("a.txt", A), ("b.txt", B))]
    fake_transport(monkeypatch, output=A + B)
    payload = _start(client, role_urls, owner, project_id, headers, {"goal": GOAL, "allowed_source_ids": ids})
    result = ge.materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    verdict = rows(Database(role_urls["aether_verifier"]), owner, project_id, "SELECT verdict, reason FROM verification_results WHERE task_id = :t", t=payload["task_id"])
    detail = f"两份资料一个说 60 一个说 90，答案两个都写：任务 {result['status']}，验收 {verdict}"
    assert result["status"] != "SUCCEEDED", detail


@pytest.mark.parametrize(
    ("first", "second", "goal", "answer"),
    [
        ("乙服务的请求超时时间是30秒。", "乙服务的请求超时时间是45秒。", "根据资料，乙服务的请求超时时间是多少秒？", "资料记载：乙服务的请求超时时间是30秒。乙服务的请求超时时间是45秒。"),
        ("丙仓库每天最多发出200件包裹。", "丙仓库每天最多发出二百五十件包裹。", "根据资料，丙仓库每天最多发出多少件包裹？", "丙仓库每天最多发出200件包裹。丙仓库每天最多发出二百五十件包裹。"),
    ],
    ids=["timeout_seconds", "daily_parcels"],
)
def test_other_contradictions_are_not_verified_as_success(client, role_urls, monkeypatch, first, second, goal, answer) -> None:
    """The conflict check is about values bound to the same subject, unit and period, not about one sample sentence."""
    owner, project_id, headers = project(client)
    ids = [_upload(client, project_id, headers, name, body) for name, body in (("x.txt", first), ("y.txt", second))]
    fake_transport(monkeypatch, output=answer)
    payload = _start(client, role_urls, owner, project_id, headers, {"goal": goal, "allowed_source_ids": ids})
    result = ge.materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    verdict = rows(Database(role_urls["aether_verifier"]), owner, project_id, "SELECT verdict, reason FROM verification_results WHERE task_id = :t", t=payload["task_id"])
    assert result["status"] != "SUCCEEDED", f"{first} / {second}：任务 {result['status']}，验收 {verdict}"
    assert all(row["verdict"] != "PASS" for row in verdict), verdict


def test_the_same_value_written_two_ways_is_not_a_contradiction(client, role_urls, monkeypatch) -> None:
    """60 and 六十 are one value. Agreeing sources must still allow a correct answer to succeed."""
    owner, project_id, headers = project(client)
    same = "甲接口每分钟最多接受六十次请求。"
    ids = [_upload(client, project_id, headers, name, body) for name, body in (("a.txt", A), ("b.txt", same))]
    fake_transport(monkeypatch, output=f"资料记载：{A}{same}")
    payload = _start(client, role_urls, owner, project_id, headers, {"goal": GOAL, "allowed_source_ids": ids})
    result = ge.materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    verdict = rows(Database(role_urls["aether_verifier"]), owner, project_id, "SELECT verdict, reason FROM verification_results WHERE task_id = :t", t=payload["task_id"])
    assert result["status"] == "SUCCEEDED", f"任务 {result}，验收 {verdict}"


def test_a_source_deleted_before_the_verification_retry_is_not_a_pass(client, role_urls, monkeypatch) -> None:
    """S-052: the first verification attempt crashed; the owner then deleted the source the answer came from."""
    owner, project_id, headers = project(client)
    source = _upload(client, project_id, headers, "fact.txt", A)
    _upload(client, project_id, headers, "other.txt", "丁服务的维护窗口在每周二凌晨。")
    state = fake_transport(monkeypatch, output=f"资料记载：{A}")
    original = ge._ensure_verification

    def unavailable(*_args, **_kwargs):
        raise OSError("verifier unavailable")

    monkeypatch.setattr(ge, "_ensure_verification", unavailable)
    payload = _start(client, role_urls, owner, project_id, headers, {"goal": GOAL})
    live = endpoints(role_urls, store_for(client, role_urls).root)
    with pytest.raises(OSError):
        ge.materialize_goal_effect(live, payload)
    deleted = client.delete(f"/v1/projects/{project_id}/artifacts/{source}", headers={**headers, "Idempotency-Key": key()})
    assert deleted.status_code == 204, deleted.text
    monkeypatch.setattr(ge, "_ensure_verification", original)
    result = ge.execute_task_effect(live, payload)
    verdict = rows(Database(role_urls["aether_verifier"]), owner, project_id, "SELECT verdict, reason FROM verification_results WHERE task_id = :t", t=payload["task_id"])
    assert state["calls"] == 1
    assert result["status"] != "SUCCEEDED", f"任务 {result}，验收 {verdict}"
    assert all(row["verdict"] != "PASS" for row in verdict), verdict
