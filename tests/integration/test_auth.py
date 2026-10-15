import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from aether_api.app import create_app
from aether_api.settings import Settings
from tests.integration.conftest import insert_task

ORIGIN = {"Origin": "http://127.0.0.1:3000"}
PASSPHRASE = "correct horse battery"


def _bootstrap(client: TestClient, name: str = "所有者") -> dict:
    response = client.post(
        "/v1/auth/bootstrap",
        headers=ORIGIN,
        json={"bootstrap_secret": client.bootstrap_secret, "display_name": name, "passphrase": PASSPHRASE},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "password" not in json.dumps(body)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    return body


def test_bootstrap_rejects_non_loopback(role_urls, clean_db, tmp_path) -> None:
    path = tmp_path / "bootstrap.json"
    settings = Settings(
        database_url=role_urls["aether_api"],
        bootstrap_path=path,
        allow_testclient=False,
        origins=frozenset({"http://127.0.0.1:3000"}),
        cookie_secure=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/auth/bootstrap",
            headers=ORIGIN,
            json={"bootstrap_secret": "x" * 20, "display_name": "所有者", "passphrase": PASSPHRASE},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "bootstrap_not_loopback"


def test_project_list_does_not_rotate_csrf(client: TestClient) -> None:
    created = _bootstrap(client)
    original = created["csrf_token"]
    listed = client.get("/v1/projects")
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    created_project = client.post(
        "/v1/projects",
        headers={**ORIGIN, "Idempotency-Key": "csrf-stable-key", "X-CSRF-Token": original},
        json={"name": "甲"},
    )
    assert created_project.status_code == 201, created_project.text


def test_session_csrf_origin_and_revoke(client: TestClient) -> None:
    created = _bootstrap(client)
    again = client.post(
        "/v1/auth/bootstrap",
        headers=ORIGIN,
        json={"bootstrap_secret": client.bootstrap_secret, "display_name": "另一个", "passphrase": PASSPHRASE},
    )
    assert again.status_code == 409

    missing_origin = client.post("/v1/projects", headers={"Idempotency-Key": "project-key-1", "X-CSRF-Token": created["csrf_token"]}, json={"name": "甲"})
    assert missing_origin.status_code == 403

    missing_csrf = client.post("/v1/projects", headers={**ORIGIN, "Idempotency-Key": "project-key-1"}, json={"name": "甲"})
    assert missing_csrf.status_code == 403

    created_project = client.post(
        "/v1/projects",
        headers={**ORIGIN, "Idempotency-Key": "project-key-1", "X-CSRF-Token": created["csrf_token"]},
        json={"name": "甲"},
    )
    assert created_project.status_code == 201, created_project.text
    replay = client.post(
        "/v1/projects",
        headers={**ORIGIN, "Idempotency-Key": "project-key-1", "X-CSRF-Token": created["csrf_token"]},
        json={"name": "甲"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == created_project.json()["id"]
    conflict = client.post(
        "/v1/projects",
        headers={**ORIGIN, "Idempotency-Key": "project-key-1", "X-CSRF-Token": created["csrf_token"]},
        json={"name": "乙"},
    )
    assert conflict.status_code == 409

    listed = client.get("/v1/projects")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["items"]] == ["甲"]

    refreshed = client.get("/v1/auth/session")
    assert refreshed.status_code == 200
    stale = client.post(
        "/v1/projects",
        headers={**ORIGIN, "Idempotency-Key": "project-key-2", "X-CSRF-Token": created["csrf_token"]},
        json={"name": "乙"},
    )
    assert stale.status_code == 403
    second = client.post(
        "/v1/projects",
        headers={**ORIGIN, "Idempotency-Key": "project-key-2", "X-CSRF-Token": refreshed.json()["csrf_token"]},
        json={"name": "乙"},
    )
    assert second.status_code == 201, second.text

    logout = client.delete("/v1/auth/session", headers={**ORIGIN, "X-CSRF-Token": refreshed.json()["csrf_token"]})
    assert logout.status_code == 204
    assert client.get("/v1/auth/session").status_code == 401


def test_login_rate_limit_and_expiry(client: TestClient, aether_admin: str) -> None:
    _bootstrap(client)
    client.cookies.clear()
    for _ in range(5):
        failed = client.post("/v1/auth/session", headers=ORIGIN, json={"passphrase": "wrong-passphrase"})
        assert failed.status_code == 401
    blocked = client.post("/v1/auth/session", headers=ORIGIN, json={"passphrase": PASSPHRASE})
    assert blocked.status_code == 429

    client.app.state.login_failures.clear()
    logged = client.post("/v1/auth/session", headers=ORIGIN, json={"passphrase": PASSPHRASE})
    assert logged.status_code == 200, logged.text
    import psycopg

    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 minute'")
    assert client.get("/v1/auth/session").status_code == 401


def test_idle_timeout(client: TestClient, aether_admin: str) -> None:
    _bootstrap(client)
    import psycopg

    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute("UPDATE auth_sessions SET last_seen_at = now() - interval '31 minutes'")
    assert client.get("/v1/auth/session").status_code == 401


def test_project_scope_does_not_leak(client: TestClient, api_db) -> None:
    _bootstrap(client)
    headers = {**ORIGIN, "X-CSRF-Token": client.get("/v1/auth/session").json()["csrf_token"]}
    first = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "scope-key-001"}, json={"name": "项目甲"})
    second = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "scope-key-002"}, json={"name": "项目乙"})
    assert first.status_code == 201 and second.status_code == 201
    owner_id = client.get("/v1/auth/session").json()["owner_id"]
    task_a = insert_task(api_db, owner_id, first.json()["id"], "甲的任务")
    insert_task(api_db, owner_id, second.json()["id"], "乙的任务")

    with api_db.transaction(owner_id=owner_id, project_id=second.json()["id"]) as conn:
        leaked = conn.execute(text("SELECT id::text FROM tasks WHERE id = :id"), {"id": task_a}).first()
    assert leaked is None
    with api_db.transaction() as conn:
        leftover = conn.execute(text("SELECT id FROM tasks")).first()
    assert leftover is None
    listed = client.get("/v1/projects")
    assert {item["id"] for item in listed.json()["items"]} == {first.json()["id"], second.json()["id"]}
    assert str(uuid.uuid4()) not in {item["id"] for item in listed.json()["items"]}
