"""P1-08 · Alembic 迁移链和 bootstrap 脚本建出的库不一致。

不变量 INV-STAB-04：任何受支持的建库/升级路径都得到同一份 schema、策略和授权。
"""

from __future__ import annotations

import psycopg
from alembic import command
from alembic.config import Config

DATABASE = "aether_audit_alembic"


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


def test_alembic_head_builds_the_same_database_as_bootstrap(role_urls, admin_url) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (DATABASE,))
        conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(DATABASE)))
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(DATABASE)))
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    cfg = Config("alembic.ini")
    cfg.set_main_option(
        "sqlalchemy.url",
        f"postgresql+psycopg://{info['user']}:{info['password']}@{info.get('host', '127.0.0.1')}:{info.get('port', 5432)}/{DATABASE}".replace("%", "%%"),
    )
    try:
        command.upgrade(cfg, "head")
        bootstrap = _shape(_url(admin_url, "aether"))
        alembic = _shape(_url(admin_url, DATABASE))
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (DATABASE,))
            conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(DATABASE)))
    missing = {key: sorted(bootstrap[key] - alembic[key]) for key in bootstrap}
    extra = {key: sorted(alembic[key] - bootstrap[key]) for key in bootstrap}
    detail = f"alembic head 缺少：{missing}\nalembic head 多出：{extra}"
    assert not any(missing.values()) and not any(extra.values()), detail
