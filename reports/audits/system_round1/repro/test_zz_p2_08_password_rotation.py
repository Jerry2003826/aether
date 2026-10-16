"""P2-08 · README 给出的 ensure_database 默认轮换所有角色口令，正在运行的 API/worker 立刻连不上库。

不变量 INV-STAB-06：重复执行建库/授权脚本不能让在跑的服务失去数据库连接。
文件名以 zz 开头，保证最后运行；用例结束时把口令恢复成会话 fixture 里的值。
"""

from __future__ import annotations

import psycopg
from psycopg import sql
from sqlalchemy import text

from aether_storage.bootstrap import ensure_database
from aether_storage.db import Database


def test_rerunning_bootstrap_does_not_lock_out_running_services(role_urls, admin_url) -> None:
    api = Database(role_urls["aether_api"])
    with api.transaction() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    error = None
    try:
        ensure_database(admin_url)
        try:
            with api.transaction() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    finally:
        info = psycopg.conninfo.conninfo_to_dict(admin_url)
        info["dbname"] = "aether"
        with psycopg.connect(psycopg.conninfo.make_conninfo(**info), autocommit=True) as conn:
            for url in role_urls.values():
                parts = psycopg.conninfo.conninfo_to_dict(url)
                conn.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(parts["user"]), sql.Literal(parts["password"])))
    assert error is None, f"再次执行 ensure_database 之后，已在运行的 API 角色连接失败：{error}"
