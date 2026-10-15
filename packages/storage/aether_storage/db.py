"""Transaction-local row security context. Settings do not survive commit."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


class Database:
    def __init__(self, url: str) -> None:
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
        self.engine: Engine = create_engine(url, pool_pre_ping=True, future=True)

    @contextmanager
    def transaction(self, *, owner_id: str | None = None, project_id: str | None = None) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            conn.execute(text("SELECT set_config('aether.owner_id', :owner_id, true)"), {"owner_id": owner_id or ""})
            conn.execute(text("SELECT set_config('aether.project_id', :project_id, true)"), {"project_id": project_id or ""})
            yield conn

    def fetch_one(self, conn: Connection, statement: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        row = conn.execute(text(statement), params or {}).mappings().first()
        return None if row is None else dict(row)

    def fetch_all(self, conn: Connection, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(text(statement), params or {}).mappings()]
