"""Create the contract database and least-privilege roles. Passwords are generated, not stored in git."""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote_plus

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = ROOT / "migrations" / "sql"
ROLES = ("aether_api", "aether_worker", "aether_verifier", "aether_dispatcher")


def _execute_script(conn: psycopg.Connection, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return row is not None


def ensure_database(admin_url: str, database: str = "aether") -> dict[str, str]:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    admin = psycopg.conninfo.conninfo_to_dict(admin_url)
    admin["dbname"] = database
    urls: dict[str, str] = {}
    with psycopg.connect(psycopg.conninfo.make_conninfo(**admin), autocommit=True) as conn:
        passwords = {role: secrets.token_urlsafe(24) for role in ROLES}
        for role, password in passwords.items():
            if _role_exists(conn, role):
                conn.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            else:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        applied = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'owners'"
        ).fetchone()
        if applied is None:
            _execute_script(conn, SQL_DIR / "0001_contract.sql")
            _execute_script(conn, SQL_DIR / "0002_privileges.sql")
        grants = (SQL_DIR / "0003_grants.sql").read_text(encoding="utf-8")
        conn.execute(grants)
        host = admin.get("host") or "127.0.0.1"
        port = admin.get("port") or 5432
        for role, password in passwords.items():
            urls[role] = f"postgresql://{quote_plus(role)}:{quote_plus(password)}@{host}:{port}/{database}"
    return urls
