"""Shared PostgreSQL roles for integration tests. The API role is not a superuser."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from aether_api.app import create_app
from aether_api.auth_service import issue_bootstrap_secret
from aether_api.settings import Settings
from aether_storage.bootstrap import ensure_database
from aether_storage.db import Database


def _admin_url() -> str:
    url = os.environ.get("AETHER_ADMIN_DATABASE_URL")
    if url:
        return url
    if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
        pytest.fail("AETHER_ADMIN_DATABASE_URL is required")
    pytest.skip("AETHER_ADMIN_DATABASE_URL unset; integration not_run")


@pytest.fixture(scope="session")
def role_urls() -> dict[str, str]:
    return ensure_database(_admin_url())


@pytest.fixture
def admin_url() -> str:
    return _admin_url()


@pytest.fixture
def aether_admin(admin_url: str) -> str:
    return _aether_admin_url(admin_url)


@pytest.fixture
def api_db(role_urls: dict[str, str]) -> Database:
    return Database(role_urls["aether_api"])


def _aether_admin_url(admin_url: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = "aether"
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture
def clean_db(admin_url: str) -> Iterator[None]:
    with psycopg.connect(_aether_admin_url(admin_url), autocommit=True) as conn:
        conn.execute("TRUNCATE idempotency_keys, owners RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def client(role_urls: dict[str, str], clean_db: None, tmp_path: Path) -> Iterator[TestClient]:
    bootstrap_path = Path("secrets/bootstrap.json")
    secret = issue_bootstrap_secret(bootstrap_path)
    settings = Settings(
        database_url=role_urls["aether_api"],
        bootstrap_path=bootstrap_path,
        allow_testclient=True,
        origins=frozenset({"http://127.0.0.1:3000"}),
        cookie_secure=False,
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.bootstrap_secret = secret  # type: ignore[attr-defined]
        try:
            yield test_client
        finally:
            bootstrap_path.unlink(missing_ok=True)


def insert_task(database: Database, owner_id: str, project_id: str, goal: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "INSERT INTO tasks (project_id, original_goal, workflow_id) "
                "VALUES (:project_id, :goal, :workflow_id) RETURNING id::text"
            ),
            {"project_id": project_id, "goal": goal, "workflow_id": f"task-{project_id}-{goal}"},
        ).scalar_one()
    return str(row)
