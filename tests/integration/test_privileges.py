from pathlib import Path

import psycopg


def test_new_tables_inherit_api_dml(aether_admin: str) -> None:
    script = Path("migrations/sql/0003_grants.sql").read_text(encoding="utf-8")
    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute(script)
        conn.execute("DROP TABLE IF EXISTS privilege_probe")
        conn.execute("CREATE TABLE privilege_probe (id integer)")
        allowed = conn.execute("SELECT has_table_privilege('aether_api', 'privilege_probe', 'INSERT')").fetchone()
        conn.execute("DROP TABLE privilege_probe")
    assert allowed is not None and allowed[0] is True
