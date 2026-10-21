"""Contract tests against a real PostgreSQL, started for the session with testcontainers-python.

The ledger's constraints, row locks and row-level security are checked against the real schema
(ensure_database: contracts/schema.sql and migrations/sql), never against tests/unit/ledger_sql_fake.py,
which stays frozen. The container is the PostgreSQL image CI uses, pinned by digest in
infra/images.lock.json. Without Docker the session fixture raises: these tests fail, they never skip.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from aether_storage.bootstrap import ensure_database
from aether_storage.db import Database
from testcontainers.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parents[2]


def _postgres_image() -> str:
    lock = json.loads((ROOT / "infra" / "images.lock.json").read_text(encoding="utf-8"))
    image = next(item for item in lock["images"] if item["repository"] == "pgvector/pgvector")
    return f"{image['repository']}:{image['tag']}@{image['digest']}"


@dataclass(frozen=True)
class RealPostgres:
    admin_url: str
    role_urls: dict[str, str]

    def aether_admin(self) -> psycopg.Connection:
        info = psycopg.conninfo.conninfo_to_dict(self.admin_url)
        info["dbname"] = "aether"
        return psycopg.connect(psycopg.conninfo.make_conninfo(**info), autocommit=True)


@pytest.fixture(scope="session")
def real_postgres() -> Iterator[RealPostgres]:
    with PostgresContainer(_postgres_image(), username="postgres", password=uuid.uuid4().hex, dbname="postgres", driver=None) as container:
        admin_url = container.get_connection_url()
        yield RealPostgres(admin_url=admin_url, role_urls=ensure_database(admin_url, rotate_passwords=True))


@dataclass(frozen=True)
class Scope:
    owner_id: str
    project_id: str
    task_id: str


@pytest.fixture
def new_scope(real_postgres: RealPostgres):
    """A fresh owner, project and task, written as the admin (row-level security does not apply to it)."""

    def make() -> Scope:
        with real_postgres.aether_admin() as conn:
            owner_id = conn.execute("INSERT INTO owners (display_name, password_hash) VALUES ('contract', 'x') RETURNING id::text").fetchone()[0]
            project_id = conn.execute("INSERT INTO projects (owner_id, name) VALUES (%s, 'contract') RETURNING id::text", (owner_id,)).fetchone()[0]
            task_id = conn.execute(
                "INSERT INTO tasks (project_id, original_goal, workflow_id) VALUES (%s, 'contract', %s) RETURNING id::text",
                (project_id, f"contract-{uuid.uuid4()}"),
            ).fetchone()[0]
        return Scope(owner_id=owner_id, project_id=project_id, task_id=task_id)

    return make


@pytest.fixture
def worker_db(real_postgres: RealPostgres) -> Database:
    return Database(real_postgres.role_urls["aether_worker"])
