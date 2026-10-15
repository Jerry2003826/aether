"""Lockfile SBOM, backup, and restore. Restore does not delete volumes."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql

SBOM_TABLES = ("projects", "artifacts", "tasks", "command_outbox")
# tasks before artifacts: artifacts.task_id references tasks.
RESTORE_ORDER = ("projects", "tasks", "artifacts", "command_outbox")
if set(RESTORE_ORDER) != set(SBOM_TABLES):
    raise RuntimeError("restore_order_mismatch")


def _packages_from_uv_lock(path: Path) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    name = ""
    version = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("name = "):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("version = ") and name:
            version = line.split("=", 1)[1].strip().strip('"')
            packages.append({"name": name, "version": version, "license": "unknown", "source": "uv.lock"})
            name = ""
            version = ""
    return packages


def _packages_from_pnpm(path: Path) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("@") or stripped[:1].isalpha():
            if "@" in stripped and stripped.endswith(":") and "registry" not in stripped:
                spec = stripped[:-1]
                name, _, version = spec.rpartition("@")
                if name and version and "/" not in version:
                    packages.append({"name": name, "version": version, "license": "unknown", "source": "pnpm-lock.yaml"})
    return packages


def build_sbom(root: Path) -> dict[str, Any]:
    components = _packages_from_uv_lock(root / "uv.lock")
    web_lock = root / "apps" / "web" / "pnpm-lock.yaml"
    if web_lock.is_file():
        components.extend(_packages_from_pnpm(web_lock))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": components,
        "vulnerabilities": {"mode": "offline", "status": "not_run", "reason": "没有配置漏洞库，空列表不是已审阅。"},
    }


def backup_project(database_url: str, artifact_root: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    snapshot: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(database_url) as conn:
        for table in SBOM_TABLES:
            rows = conn.execute(f"SELECT to_jsonb(t) FROM {table} AS t").fetchall()  # table name is a fixed tuple
            snapshot[table] = [row[0] for row in rows]
        pending = conn.execute("SELECT id::text FROM command_outbox WHERE status IN ('PENDING', 'DELIVERING')").fetchall()
    (destination / "tables.json").write_text(json.dumps(snapshot, default=str), encoding="utf-8")
    artifact_copy = destination / "artifacts"
    if artifact_root.is_dir():
        shutil.copytree(artifact_root, artifact_copy, dirs_exist_ok=True)
    digest = hashlib.sha256((destination / "tables.json").read_bytes()).hexdigest()
    manifest = {"sha256": digest, "pending_commands": [row[0] for row in pending], "volume_deleted": False}
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _install_tree(source: Path, destination: Path) -> None:
    """Copy into a sibling directory, then rename it into place.

    The live tree stays until that copy has a name on the same filesystem.
    """
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staging = parent / f".{destination.name}.restore-{token}"
    previous = parent / f".{destination.name}.previous-{token}"
    try:
        shutil.copytree(source, staging)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    try:
        if destination.exists():
            destination.rename(previous)
        staging.rename(destination)
    except Exception:
        if not destination.exists() and previous.exists():
            previous.rename(destination)
        if staging.exists():
            shutil.rmtree(staging)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def _primary_key(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT attribute.attname
        FROM pg_index AS index
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = index.indrelid AND attribute.attnum = ANY (index.indkey)
        WHERE index.indrelid = %s::regclass AND index.indisprimary
        ORDER BY array_position(index.indkey, attribute.attnum)
        """,
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


def _upsert_row(conn: psycopg.Connection, table: str, row: dict[str, Any]) -> None:
    columns = [
        item[0]
        for item in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()
    ]
    primary_key = _primary_key(conn, table)
    if not primary_key or not columns:
        raise RuntimeError(f"restore_table_rejected:{table}")
    assignments = sql.SQL(", ").join(
        sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(column))
        for column in columns
        if column not in primary_key
    )
    statement = sql.SQL(
        """
        INSERT INTO {table}
        SELECT * FROM jsonb_populate_record(NULL::{table}, %s::jsonb)
        ON CONFLICT ({primary_key}) DO UPDATE SET {assignments}
        """
    ).format(
        table=sql.Identifier(table),
        primary_key=sql.SQL(", ").join(sql.Identifier(column) for column in primary_key),
        assignments=assignments,
    )
    conn.execute(statement, (json.dumps(row),))


def _rows_in_restore_order(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if table != "tasks":
        return rows
    pending = {str(row["id"]): row for row in rows}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(row: dict[str, Any]) -> None:
        identity = str(row["id"])
        if identity in seen:
            return
        parent = row.get("parent_task_id")
        if parent is not None and str(parent) in pending:
            visit(pending[str(parent)])
        seen.add(identity)
        ordered.append(row)

    for row in rows:
        visit(row)
    return ordered


def restore_project(database_url: str, artifact_root: Path, source: Path, *, confirm: str) -> dict[str, Any]:
    """Write snapshot rows back, stop unfinished commands, then put artifact bytes back."""
    if confirm != "restore-aether":
        raise RuntimeError("restore_not_confirmed")
    recorded = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    raw_tables = (source / "tables.json").read_bytes()
    digest = hashlib.sha256(raw_tables).hexdigest()
    if digest != recorded["sha256"]:
        raise RuntimeError("backup_hash_mismatch")
    snapshot = json.loads(raw_tables)
    if set(snapshot) != set(SBOM_TABLES):
        raise RuntimeError("backup_tables_missing")
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            for table in RESTORE_ORDER:
                for row in _rows_in_restore_order(table, snapshot[table]):
                    _upsert_row(conn, table, row)
            # Stop after the snapshot write. Restored PENDING rows would be dispatchable again.
            stopped = conn.execute(
                """
                UPDATE command_outbox
                SET status = 'STOPPED'
                WHERE status IN ('PENDING', 'DELIVERING')
                RETURNING id::text
                """
            ).fetchall()
            pending = conn.execute(
                "SELECT count(*) FROM command_outbox WHERE status IN ('PENDING', 'DELIVERING')"
            ).fetchone()
    copied = source / "artifacts"
    if copied.is_dir():
        _install_tree(copied, artifact_root)
    return {
        "dispatch": "stopped",
        "stopped_commands": [row[0] for row in stopped],
        "pending_after_restore": int(pending[0]) if pending else 0,
        "restored_tables": {table: len(snapshot[table]) for table in RESTORE_ORDER},
        "volume_deleted": False,
    }
