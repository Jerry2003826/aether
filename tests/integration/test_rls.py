import psycopg
import pytest
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.integration.conftest import insert_task


def test_runtime_role_is_not_superuser(role_urls) -> None:
    with psycopg.connect(role_urls["aether_api"]) as conn:
        row = conn.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    assert row == (False, False)


def test_composite_fk_and_api_cannot_mark_success(client, api_db, role_urls) -> None:
    from tests.integration.test_auth import _bootstrap

    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    csrf = session["csrf_token"]
    owner_id = session["owner_id"]
    headers = {"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": csrf}
    project_a = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "fk-key-0001"}, json={"name": "甲"}).json()["id"]
    project_b = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "fk-key-0002"}, json={"name": "乙"}).json()["id"]
    task_a = insert_task(api_db, owner_id, project_a, "约束任务")
    with pytest.raises(DBAPIError) as raised:
        with api_db.transaction(owner_id=owner_id, project_id=project_b) as conn:
            conn.execute(
                text(
                    "INSERT INTO task_nodes (project_id, task_id, node_key, role, objective, state, max_turns) "
                    "VALUES (:project_id, :task_id, 'node-1', 'research', '读取', 'PENDING', 4)"
                ),
                {"project_id": project_b, "task_id": task_a},
            )
    assert isinstance(raised.value.orig, ForeignKeyViolation)

    with pytest.raises(DBAPIError) as success:
        with api_db.transaction(owner_id=owner_id, project_id=project_a) as conn:
            conn.execute(text("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = :id"), {"id": task_a})
    assert "api role cannot advance" in str(success.value)

    with pytest.raises(DBAPIError) as denied:
        with api_db.transaction(owner_id=owner_id, project_id=project_a) as conn:
            conn.execute(text("INSERT INTO verification_results DEFAULT VALUES"))
    assert isinstance(denied.value.orig, (InsufficientPrivilege, ProgrammingError))


def test_worker_can_mark_success(client, api_db, role_urls) -> None:
    from aether_storage.db import Database
    from tests.integration.test_auth import _bootstrap

    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    owner_id = session["owner_id"]
    csrf = session["csrf_token"]
    project_id = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": csrf, "Idempotency-Key": "worker-key-1"},
        json={"name": "执行"},
    ).json()["id"]
    task_id = insert_task(api_db, owner_id, project_id, "交给 worker")
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(text("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = :id"), {"id": task_id})
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    assert status == "SUCCEEDED"
