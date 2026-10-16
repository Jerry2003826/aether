"""System audit round 1, P2-08: rerunning ensure_database must not change the passwords of running services.

INV-STAB-06: repeating the create/grant script cannot cut a running API or worker off the database.
Rotation stays available, but only when the caller asks for it.
"""

from __future__ import annotations

import psycopg
from psycopg import sql
from sqlalchemy import text

from aether_storage.bootstrap import ROLES, ensure_database
from aether_storage.db import Database


def _restore_passwords(admin_url: str, role_urls: dict[str, str]) -> None:
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = "aether"
    with psycopg.connect(psycopg.conninfo.make_conninfo(**info), autocommit=True) as conn:
        for url in role_urls.values():
            parts = psycopg.conninfo.conninfo_to_dict(url)
            conn.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(parts["user"]), sql.Literal(parts["password"])))


def test_rerunning_bootstrap_does_not_lock_out_running_services(role_urls, admin_url) -> None:
    api = Database(role_urls["aether_api"])
    with api.transaction() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    error = None
    returned: dict[str, str] | None = None
    try:
        returned = ensure_database(admin_url)
        try:
            with api.transaction() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    finally:
        _restore_passwords(admin_url, role_urls)
    assert error is None, f"再次执行 ensure_database 之后，已在运行的 API 角色连接失败：{error}"
    assert returned == {}, f"默认调用不应为已存在的角色生成新口令：{sorted(returned or {})}"


def test_rotation_is_explicit_and_returns_a_working_url_for_every_role(role_urls, admin_url) -> None:
    try:
        rotated = ensure_database(admin_url, rotate_passwords=True)
        assert sorted(rotated) == sorted(ROLES)
        for role, url in rotated.items():
            assert url != role_urls[role]
            with Database(url).transaction() as conn:
                assert conn.execute(text("SELECT current_user")).scalar_one() == role
    finally:
        _restore_passwords(admin_url, role_urls)
    with Database(role_urls["aether_api"]).transaction() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
