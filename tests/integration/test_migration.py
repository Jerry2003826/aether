"""Alembic upgrade, data-preserving column add, rollback, and upgrade again."""

from __future__ import annotations

import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aether_storage.bootstrap import ALEMBIC_HEAD, ensure_database


def _admin_url() -> str:
    url = os.environ.get("AETHER_ADMIN_DATABASE_URL")
    if not url:
        if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
            pytest.fail("AETHER_ADMIN_DATABASE_URL is required")
        pytest.skip("AETHER_ADMIN_DATABASE_URL unset; migration not_run")
    return url


def test_upgrade_preserves_rows_and_can_roll_back() -> None:
    admin = _admin_url()
    database = "aether_alembic_case"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database,))
        conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(database)))
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(database)))
    info = psycopg.conninfo.conninfo_to_dict(admin)
    info["dbname"] = database
    sqlalchemy_url = (
        f"postgresql+psycopg://{info['user']}:{info['password']}@{info.get('host', '127.0.0.1')}:"
        f"{info.get('port', 5432)}/{database}"
    )
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    command.upgrade(cfg, "0001")
    with psycopg.connect(psycopg.conninfo.make_conninfo(**info), autocommit=True) as conn:
        owner_id = conn.execute("INSERT INTO owners (display_name, password_hash) VALUES ('保留', 'not-a-login') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO projects (owner_id, name) VALUES (%s, '保留项目')", (owner_id,))
    command.upgrade(cfg, "0002")
    with psycopg.connect(psycopg.conninfo.make_conninfo(**info)) as conn:
        row = conn.execute("SELECT name, display_note FROM projects").fetchone()
    assert row == ("保留项目", None)
    command.downgrade(cfg, "0001")
    with psycopg.connect(psycopg.conninfo.make_conninfo(**info)) as conn:
        assert conn.execute("SELECT name FROM projects").fetchone() == ("保留项目",)
        assert conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'projects' AND column_name = 'display_note'").fetchone() is None
    command.downgrade(cfg, "base")
    with psycopg.connect(psycopg.conninfo.make_conninfo(**info)) as conn:
        assert conn.execute("SELECT to_regclass('public.owners')").fetchone()[0] is None
    command.upgrade(cfg, "head")
    with psycopg.connect(psycopg.conninfo.make_conninfo(**info)) as conn:
        assert conn.execute("SELECT to_regclass('public.projects')").fetchone()[0] == "projects"
        assert conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'projects' AND column_name = 'display_note'").fetchone()[0] == "display_note"
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == ALEMBIC_HEAD


def test_bootstrap_stamp_lets_later_upgrade_continue(role_urls, admin_url: str) -> None:
    database = "aether_stamp_case"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        )
        conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(database)))
    ensure_database(admin_url, database=database, rotate_passwords=False)
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = database
    sqlalchemy_url = (
        f"postgresql+psycopg://{info['user']}:{info['password']}@{info.get('host', '127.0.0.1')}:"
        f"{info.get('port', 5432)}/{database}"
    )
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(**info)) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == ALEMBIC_HEAD
            assert conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'projects' AND column_name = 'display_note'"
            ).fetchone()[0] == "display_note"
        command.upgrade(cfg, "head")
        with psycopg.connect(psycopg.conninfo.make_conninfo(**info), autocommit=True) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == ALEMBIC_HEAD
            conn.execute("DELETE FROM alembic_version")
        with pytest.raises(Exception) as unstamped:
            command.upgrade(cfg, "head")
        assert "already exists" in str(unstamped.value).lower()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(database)))
