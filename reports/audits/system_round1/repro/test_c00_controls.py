"""对照组：在基线上应当通过。用来证明复现夹具本身能区分"守住了"和"没守住"。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from audit_helpers import endpoints, fake_transport, key, project, second_project, store_for


def test_control_cross_project_source_is_rejected_before_any_call(client, role_urls, monkeypatch) -> None:
    _owner, project_id, headers = project(client)
    other = second_project(client, headers)
    foreign = client.post(
        f"/v1/projects/{other}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("x.txt", "别的项目的资料：每分钟六十次。".encode(), "text/plain")},
    )
    assert foreign.status_code == 201, foreign.text
    state = fake_transport(monkeypatch, output="每分钟六十次。")
    created = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**headers, "Idempotency-Key": key()},
        json={"goal": "根据资料回答每分钟多少次", "allowed_source_ids": [foreign.json()["artifact_id"]]},
    )
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    result = materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root), payload)
    assert state["calls"] == 0
    assert result["status"] == "NEEDS_REVIEW" and result["stop_reason"] == "allowed_sources_invalid"


def test_control_api_role_cannot_write_verifier_evidence(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "权限对照"})
    task_id = created.json()["resource_id"]
    api = Database(role_urls["aether_api"])
    with pytest.raises(DBAPIError):
        with api.transaction(owner_id=owner, project_id=project_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO verification_results (project_id, task_id, contract_id, artifact_id, criterion_id, verdict, artifact_sha256, "
                    "contract_sha256, verifier_identity, report_artifact_id, reason, checked_at) VALUES "
                    "(:p, :t, gen_random_uuid(), gen_random_uuid(), 'x', 'PASS', :h, :h, 'api', gen_random_uuid(), 'forged', now())"
                ),
                {"p": project_id, "t": task_id, "h": "a" * 64},
            )
    with pytest.raises(DBAPIError):
        with api.transaction(owner_id=owner, project_id=project_id) as conn:
            conn.execute(text("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = :id"), {"id": task_id})


def test_control_rls_hides_rows_without_the_project_setting(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "RLS 对照"})
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner) as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar_one() == 0
    other = second_project(client, headers)
    with worker.transaction(owner_id=owner, project_id=other) as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks WHERE project_id = :p"), {"p": project_id}).scalar_one() == 0
