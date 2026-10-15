"""Transaction-local row security context. Settings do not survive commit."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool


class Database:
    def __init__(self, url: str) -> None:
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
        self.engine: Engine = create_engine(url, poolclass=NullPool, future=True)

    @contextmanager
    def transaction(self, *, owner_id: str | None = None, project_id: str | None = None) -> Iterator[Connection]:
        """Commit the database transaction before any after-commit callback runs.

        Callbacks are how ArtifactStore publishes a journal line only after PostgreSQL
        has accepted the commit. A commit exception is not a rollback and not a commit.
        """
        after_commit: list[Callable[[], None]] = []
        after_rollback: list[Callable[[], None]] = []
        after_unknown: list[Callable[[], None]] = []
        with self.engine.connect() as conn:
            conn.info["aether_after_commit"] = after_commit
            conn.info["aether_after_rollback"] = after_rollback
            conn.info["aether_after_unknown"] = after_unknown
            trans = conn.begin()
            try:
                conn.execute(text("SELECT set_config('aether.owner_id', :owner_id, true)"), {"owner_id": owner_id or ""})
                conn.execute(text("SELECT set_config('aether.project_id', :project_id, true)"), {"project_id": project_id or ""})
                yield conn
            except Exception:
                if trans.is_active:
                    trans.rollback()
                _run_quietly(after_rollback)
                raise
            else:
                try:
                    trans.commit()
                except Exception:
                    _run_quietly(after_unknown)
                    _release_dbapi(conn)
                    raise
                _run_callbacks(after_commit)


def _run_callbacks(callbacks: list[Callable[[], None]]) -> None:
    for callback in list(callbacks):
        callback()


def _run_quietly(callbacks: list[Callable[[], None]]) -> None:
    for callback in list(callbacks):
        try:
            callback()
        except Exception:
            continue


def _release_dbapi(conn: Connection) -> None:
    """Roll back a transaction PostgreSQL may still be holding after a failed commit ack.

    A commit that already reached the server is not undone by a later rollback.
    """
    try:
        dbapi = conn.connection
        rollback = getattr(dbapi, "rollback", None)
        if rollback is not None:
            rollback()
    except Exception:
        return

    def fetch_one(self, conn: Connection, statement: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        row = conn.execute(text(statement), params or {}).mappings().first()
        return None if row is None else dict(row)

    def fetch_all(self, conn: Connection, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(text(statement), params or {}).mappings()]
