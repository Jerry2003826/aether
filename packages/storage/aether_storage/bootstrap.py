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
ALEMBIC_HEAD = "0010"
_BEFORE_HEAD = ("0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009")


def _execute_script(conn: psycopg.Connection, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return row is not None


def _stamp_alembic_head(conn: psycopg.Connection) -> None:
    """Record the revision that matches the scripts applied above.

    A database created by these scripts otherwise has no alembic_version row.
    The next ``alembic upgrade head`` would replay 0001 and stop on existing tables.
    """
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS display_note text")
    _execute_script(conn, SQL_DIR / "task_status_guard.sql")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alembic_version (
          version_num varchar(32) NOT NULL,
          CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
        )
        """
    )
    rows = [row[0] for row in conn.execute("SELECT version_num FROM alembic_version").fetchall()]
    if not rows:
        conn.execute("INSERT INTO alembic_version (version_num) VALUES (%s)", (ALEMBIC_HEAD,))
        return
    if rows == [ALEMBIC_HEAD]:
        return
    if len(rows) == 1 and rows[0] in _BEFORE_HEAD:
        conn.execute("UPDATE alembic_version SET version_num = %s", (ALEMBIC_HEAD,))


def ensure_database(admin_url: str, database: str = "aether", *, rotate_passwords: bool = True) -> dict[str, str]:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    admin = psycopg.conninfo.conninfo_to_dict(admin_url)
    admin["dbname"] = database
    urls: dict[str, str] = {}
    with psycopg.connect(psycopg.conninfo.make_conninfo(**admin), autocommit=True) as conn:
        passwords: dict[str, str] = {}
        for role in ROLES:
            if _role_exists(conn, role):
                if not rotate_passwords:
                    continue
                password = secrets.token_urlsafe(24)
                conn.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            else:
                password = secrets.token_urlsafe(24)
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            passwords[role] = password
        applied = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'owners'"
        ).fetchone()
        if applied is None:
            _execute_script(conn, ROOT / "contracts" / "schema.sql")
            _execute_script(conn, SQL_DIR / "task_status_guard.sql")
            _execute_script(conn, SQL_DIR / "0002_privileges.sql")
        grants = (SQL_DIR / "0003_grants.sql").read_text(encoding="utf-8")
        conn.execute(grants)
        _execute_script(conn, SQL_DIR / "0005_runtime_patches.sql")
        _execute_script(conn, SQL_DIR / "0009_task_references.sql")
        _execute_script(conn, SQL_DIR / "0010_budget_wall.sql")
        _stamp_alembic_head(conn)
        host = admin.get("host") or "127.0.0.1"
        port = admin.get("port") or 5432
        for role, password in passwords.items():
            urls[role] = f"postgresql://{quote_plus(role)}:{quote_plus(password)}@{host}:{port}/{database}"
    return urls
