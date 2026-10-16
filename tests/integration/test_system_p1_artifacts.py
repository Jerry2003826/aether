"""P1-01: byte identity is not logical artifact identity."""

from __future__ import annotations

from aether_storage.artifacts import ArtifactError
from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from tests.integration.goal_approval import record_owner_approval
from tests.integration.system_audit import FACT_GOAL, answered_task, key, project, store_for


def test_uploading_a_previous_answer_gives_a_usable_project_source(client, role_urls) -> None:
    _owner, project_id, headers, answer, scripted = answered_task(client, role_urls, delete_answer=False)
    data = client.get(f"/v1/projects/{project_id}/artifacts/{answer}")
    assert data.status_code == 200, data.text
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("previous-answer.txt", data.content, "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    created = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**headers, "Idempotency-Key": key()},
        json={"goal": "根据资料，水在标准大气压下的沸点是多少？", "allowed_source_ids": [body["artifact_id"]]},
    )
    assert created.status_code == 202, created.text
    follow = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    record_owner_approval(Database(role_urls["aether_worker"]), owner_id=_owner, project_id=project_id, task_id=follow["task_id"])
    result = materialize_goal_effect(scripted, follow)
    detail = f"上传返回 id={body['artifact_id']} classification={body['classification']}（原答案 id={answer}）；用它当来源的任务={result}"
    assert body["classification"] == "project_private", detail
    assert body["artifact_id"] != answer, detail
    assert result.get("stop_reason") != "allowed_sources_invalid", detail


def test_deleting_an_upload_does_not_delete_another_tasks_answer(client, role_urls) -> None:
    owner, project_id, headers, answer, _scripted = answered_task(client, role_urls, delete_answer=False)
    data = client.get(f"/v1/projects/{project_id}/artifacts/{answer}").content
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("copy.txt", data, "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    removed = client.delete(f"/v1/projects/{project_id}/artifacts/{uploaded.json()['artifact_id']}", headers={**headers, "Idempotency-Key": key()})
    assert removed.status_code == 204, removed.text
    error = None
    try:
        store_for(client, role_urls).get(owner_id=owner, project_id=project_id, artifact_id=answer)
    except ArtifactError as exc:
        error = exc.code
    assert error is None, f"删除用户自己的上传后，另一个任务的答案 {answer} 读不到了：{error}"


def test_a_deleted_answer_does_not_break_a_later_identical_answer(client, role_urls) -> None:
    _owner, project_id, headers, _answer, scripted = answered_task(client, role_urls, delete_answer=True)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": FACT_GOAL})
    assert created.status_code == 202, created.text
    second = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    record_owner_approval(Database(role_urls["aether_worker"]), owner_id=_owner, project_id=project_id, task_id=second["task_id"])
    error: BaseException | None = None
    result: dict = {}
    try:
        result = materialize_goal_effect(scripted, second)
    except Exception as exc:
        error = exc
    assert error is None, f"第二个任务写同样的答案字节时抛出 {type(error).__name__}: {error}"
    assert result["status"] in {"SUCCEEDED", "NEEDS_REVIEW", "FAILED"}, result


def test_reuploading_deleted_bytes_is_a_new_explicit_write(client, role_urls) -> None:
    _owner, project_id, headers = project(client)
    body = "一份后来被删掉、又被用户重新上传的资料。\n".encode()
    first = client.post(f"/v1/projects/{project_id}/artifacts", headers={**headers, "Idempotency-Key": key()}, files={"file": ("a.txt", body, "text/plain")})
    assert first.status_code == 201, first.text
    removed = client.delete(f"/v1/projects/{project_id}/artifacts/{first.json()['artifact_id']}", headers={**headers, "Idempotency-Key": key()})
    assert removed.status_code == 204, removed.text
    again = client.post(f"/v1/projects/{project_id}/artifacts", headers={**headers, "Idempotency-Key": key()}, files={"file": ("a.txt", body, "text/plain")})
    assert again.status_code == 201, f"重新上传被拒绝：{again.status_code} {again.text}"
    assert again.json()["artifact_id"] != first.json()["artifact_id"]


def test_same_bytes_with_a_different_media_type_are_a_new_artifact(client, role_urls) -> None:
    _owner, project_id, headers = project(client)
    body = "同一段字节，两种媒体类型。\n".encode()
    plain = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("note.txt", body, "text/plain")},
    )
    markdown = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("note.md", body, "text/markdown")},
    )
    assert plain.status_code == 201, plain.text
    assert markdown.status_code == 201, markdown.text
    assert plain.json()["artifact_id"] != markdown.json()["artifact_id"]
    assert markdown.json()["media_type"] == "text/markdown"
