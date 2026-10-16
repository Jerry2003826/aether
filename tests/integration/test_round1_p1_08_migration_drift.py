"""System audit round 1, P1-08: the Alembic chain and the bootstrap script must build the same database.

INV-STAB-04: every supported create or upgrade path ends with the same columns, policies and grants.
"""

from __future__ import annotations

import psycopg
import pytest
from alembic import command
from alembic.config import Config

DATABASE = "aether_round1_alembic"


def _shape(url: str) -> dict[str, set]:
    with psycopg.connect(url) as conn:
        columns = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
            ).fetchall()
        }
        policies = {(row[0], row[1]) for row in conn.execute("SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'").fetchall()}
        grants = {
            (row[0], row[1], row[2])
            for row in conn.execute(
                "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND grantee LIKE 'aether_%'"
            ).fetchall()
        }
    return {"columns": columns, "policies": policies, "grants": grants}


def _url(admin_url: str, database: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = database
    return psycopg.conninfo.make_conninfo(**info)


def _drop(admin_url: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (DATABASE,))
        conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(DATABASE)))


def _config(admin_url: str) -> Config:
    _drop(admin_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(DATABASE)))
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option(
        "sqlalchemy.url",
        f"postgresql+psycopg://{info['user']}:{info['password']}@{info.get('host', '127.0.0.1')}:{info.get('port', 5432)}/{DATABASE}".replace("%", "%%"),
    )
    return cfg


def _assert_same(admin_url: str) -> None:
    bootstrap = _shape(_url(admin_url, "aether"))
    alembic = _shape(_url(admin_url, DATABASE))
    missing = {key: sorted(bootstrap[key] - alembic[key]) for key in bootstrap}
    extra = {key: sorted(alembic[key] - bootstrap[key]) for key in bootstrap}
    detail = f"alembic head 缺少：{missing}\nalembic head 多出：{extra}"
    assert not any(missing.values()) and not any(extra.values()), detail


def test_alembic_head_builds_the_same_database_as_bootstrap(role_urls, admin_url) -> None:
    cfg = _config(admin_url)
    try:
        command.upgrade(cfg, "head")
        _assert_same(admin_url)
    finally:
        _drop(admin_url)


@pytest.mark.parametrize("revision", ["0011", "0012"])
def test_database_already_at_an_earlier_head_upgrades_to_the_bootstrap_shape(role_urls, admin_url, revision) -> None:
    cfg = _config(admin_url)
    try:
        command.upgrade(cfg, revision)
        with psycopg.connect(_url(admin_url, DATABASE)) as conn:
            assert conn.execute("SELECT to_regclass('public.resource_heads')").fetchone()[0] is None
        command.upgrade(cfg, "head")
        _assert_same(admin_url)
    finally:
        _drop(admin_url)
