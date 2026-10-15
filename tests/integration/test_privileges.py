from pathlib import Path

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.integration.test_auth import _bootstrap


AUTHORITY_TABLES = (
    "operations",
    "budget_accounts",
    "model_requests",
    "audit_events",
    "verification_results",
    "evaluation_runs",
)


def test_new_tables_do_not_inherit_dml(aether_admin: str) -> None:
    script = Path("migrations/sql/0003_grants.sql").read_text(encoding="utf-8")
    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute(script)
        conn.execute("DROP TABLE IF EXISTS privilege_probe")
        conn.execute("CREATE TABLE privilege_probe (id integer)")
        try:
            for role in ("aether_api", "aether_worker"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    allowed = conn.execute(
                        "SELECT has_table_privilege(%s, 'privilege_probe', %s)",
                        (role, privilege),
                    ).fetchone()
                    assert allowed is not None and allowed[0] is False
        finally:
            conn.execute("DROP TABLE privilege_probe")


def test_api_cannot_mutate_authority_tables(client, api_db, aether_admin: str) -> None:
    script = Path("migrations/sql/0003_grants.sql").read_text(encoding="utf-8")
    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute(script)
        conn.execute(Path("migrations/sql/0005_runtime_patches.sql").read_text(encoding="utf-8"))
        for table in AUTHORITY_TABLES:
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                allowed = conn.execute(
                    "SELECT has_table_privilege('aether_api', %s, %s)",
                    (table, privilege),
                ).fetchone()
                assert allowed is not None and allowed[0] is False, (table, privilege)
        for privilege, expected in (("INSERT", True), ("UPDATE", False), ("DELETE", False)):
            allowed = conn.execute(
                "SELECT has_table_privilege('aether_api', 'command_outbox', %s)",
                (privilege,),
            ).fetchone()
            assert allowed is not None and allowed[0] is expected
        assert conn.execute("SELECT has_table_privilege('aether_worker', 'budget_accounts', 'INSERT')").fetchone()[0] is True
        assert conn.execute("SELECT has_table_privilege('aether_worker', 'verification_results', 'INSERT')").fetchone()[0] is False

    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    with pytest.raises(DBAPIError) as denied:
        with api_db.transaction(owner_id=session["owner_id"]) as conn:
            conn.execute(text("INSERT INTO budget_accounts DEFAULT VALUES"))
    assert isinstance(denied.value.orig, InsufficientPrivilege)
