"""Lockfile SBOM, backup, and restore. Restore does not delete volumes, rows, or files."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql

# Every public table, parents before children. Columns in DEFERRED_COLUMNS close
# reference cycles, so they are written after every row exists.
SBOM_TABLES = (
    "owners",
    "auth_sessions",
    "projects",
    "idempotency_keys",
    "resource_heads",
    "connector_configs",
    "tasks",
    "goal_contracts",
    "task_nodes",
    "node_dependencies",
    "command_outbox",
    "task_events",
    "capability_grants",
    "approvals",
    "operations",
    "artifacts",
    "task_artifacts",
    "verification_results",
    "evidence_links",
    "budget_accounts",
    "model_requests",
    "sources",
    "source_versions",
    "chunks",
    "index_generations",
    "memories",
    "skills",
    "skill_versions",
    "evaluation_runs",
    "skill_deployments",
    "profile_snapshots",
    "recommendation_impressions",
    "feedback_events",
    "watches",
    "notifications",
    "audit_events",
    "ingestion_jobs",
    "coding_jobs",
)
RESTORE_ORDER = SBOM_TABLES
DEFERRED_COLUMNS = {
    "tasks": ("current_contract_id", "parent_task_id"),
    "approvals": ("consumed_by_operation_id",),
    "memories": ("supersedes_id",),
    "skill_versions": ("parent_version_id",),
}
# Rows that only record content go back to the snapshot. Every other table holds
# ledgers, lifecycle state, or security decisions made after the backup, so a
# live row that differs from the snapshot stays as it is.
CONTENT_TABLES = frozenset(
    {
        "projects",
        "artifacts",
        "task_artifacts",
        "sources",
        "source_versions",
        "chunks",
        "index_generations",
        "memories",
        "skills",
        "profile_snapshots",
        "recommendation_impressions",
        "feedback_events",
        "notifications",
    }
)
MONOTONIC_COLUMNS = frozenset({"visibility_epoch"})
# Lifecycle and security decisions inside content tables keep their live value:
# a superseded memory, a retired index generation, a reclassified artifact.
LIVE_COLUMNS = {
    "artifacts": frozenset({"classification"}),
    "task_artifacts": frozenset({"classification"}),
    "index_generations": frozenset({"state", "activated_at"}),
    "memories": frozenset({"epistemic_status"}),
}
TOMBSTONE_COLUMNS = ("deleted_at", "revoked_at")
# Artifact-store bookkeeping written after the backup describes the live store.
LIVE_ARTIFACT_DIRECTORIES = frozenset({"journal", "locks", "tmp"})
if not CONTENT_TABLES <= set(SBOM_TABLES) or not set(DEFERRED_COLUMNS) | set(LIVE_COLUMNS) <= set(SBOM_TABLES):
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


def _assert_schema_covered(conn: psycopg.Connection) -> None:
    """Refuse to back up or restore a schema whose tables or references this module does not know."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') AND relname <> 'alembic_version'"
        ).fetchall()
    }
    if tables != set(SBOM_TABLES):
        missing = sorted(tables - set(SBOM_TABLES)) + sorted(f"-{name}" for name in set(SBOM_TABLES) - tables)
        raise RuntimeError(f"backup_schema_mismatch:{','.join(missing)}")
    position = {table: index for index, table in enumerate(SBOM_TABLES)}
    references = conn.execute(
        """
        SELECT constraint_row.conrelid::regclass::text, constraint_row.confrelid::regclass::text,
               array_agg(attribute.attname::text)
        FROM pg_constraint AS constraint_row
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = constraint_row.conrelid AND attribute.attnum = ANY (constraint_row.conkey)
        WHERE constraint_row.contype = 'f' AND constraint_row.connamespace = 'public'::regnamespace
        GROUP BY constraint_row.oid, constraint_row.conrelid, constraint_row.confrelid
        """
    ).fetchall()
    for child, parent, columns in references:
        if any(column in DEFERRED_COLUMNS.get(child, ()) for column in columns):
            continue
        if position[parent] >= position[child]:
            raise RuntimeError(f"restore_order_mismatch:{child}->{parent}")


def backup_project(database_url: str, artifact_root: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    snapshot: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(database_url) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.transaction():
            _assert_schema_covered(conn)
            for table in SBOM_TABLES:
                rows = conn.execute(sql.SQL("SELECT to_jsonb(t) FROM {} AS t").format(sql.Identifier(table))).fetchall()
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


def _sibling(path: Path, label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.parent / f".{path.name}.{label}-{uuid4().hex}"


def _install_tree(source: Path, destination: Path) -> None:
    """Copy into a sibling directory, then rename it into place.

    The live tree stays until that copy has a name on the same filesystem.
    """
    staging = _sibling(destination, "restore")
    try:
        shutil.copytree(source, staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        previous = _swap_in(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if previous is not None:
        shutil.rmtree(previous)


def _stage_artifacts(snapshot: Path, artifact_root: Path) -> tuple[Path, dict[str, int]]:
    """Build the merged tree in a sibling directory; the live tree is not touched until it is swapped in."""
    staging = _sibling(artifact_root, "restore")
    try:
        if artifact_root.exists():
            shutil.copytree(artifact_root, staging, symlinks=True)
            counts = _merge_tree(snapshot, staging)
        else:
            shutil.copytree(snapshot, staging, symlinks=True)
            counts = {"restored_files": sum(1 for path in staging.rglob("*") if path.is_file()), "kept_live_files": 0}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging, counts


def _merge_tree(source: Path, destination: Path) -> dict[str, int]:
    """Put snapshot bytes into a staged copy file by file; files that exist only in the live tree stay."""
    restored = 0
    kept = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        if target.exists() and not target.is_file():
            raise RuntimeError(f"restore_path_conflict:{relative}")
        if target.is_file():
            if target.read_bytes() == path.read_bytes():
                continue
            if relative.parts[0] in LIVE_ARTIFACT_DIRECTORIES:
                kept += 1
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        restored += 1
    return {"restored_files": restored, "kept_live_files": kept}


def _swap_in(staging: Path, artifact_root: Path) -> Path | None:
    """Rename the staged tree into place and return where the live tree went, so a failed commit can put it back."""
    previous = None
    if artifact_root.exists():
        previous = _sibling(artifact_root, "previous")
        artifact_root.rename(previous)
    try:
        staging.rename(artifact_root)
    except Exception:
        if previous is not None:
            previous.rename(artifact_root)
        raise
    return previous


def _swap_back(artifact_root: Path, previous: Path | None) -> None:
    discarded = _sibling(artifact_root, "discarded")
    artifact_root.rename(discarded)
    if previous is not None:
        previous.rename(artifact_root)
    shutil.rmtree(discarded, ignore_errors=True)


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (table,),
        ).fetchall()
    ]


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


def _snapshot_rows(table: str) -> sql.Composed:
    return sql.SQL("jsonb_populate_recordset(NULL::{table}, %s::jsonb)").format(table=sql.Identifier(table))


def _same_key(primary_key: list[str]) -> sql.Composable:
    return sql.SQL(" AND ").join(
        sql.SQL("live.{column} = snapshot.{column}").format(column=sql.Identifier(column)) for column in primary_key
    )


def _key_of(row: dict[str, Any], primary_key: list[str]) -> tuple[str, ...]:
    return tuple(str(row[column]) for column in primary_key)


def _insert_missing(conn: psycopg.Connection, table: str, columns: list[str], primary_key: list[str], rows: list[dict[str, Any]]) -> set[tuple[str, ...]]:
    deferred = DEFERRED_COLUMNS.get(table, ())
    values = sql.SQL(", ").join(
        sql.SQL("NULL") if column in deferred else sql.SQL("snapshot.{}").format(sql.Identifier(column)) for column in columns
    )
    statement = sql.SQL(
        "INSERT INTO {table} ({columns}) SELECT {values} FROM {rows} AS snapshot "
        "WHERE NOT EXISTS (SELECT 1 FROM {table} AS live WHERE {same_key}) RETURNING {key}"
    ).format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        values=values,
        rows=_snapshot_rows(table),
        same_key=_same_key(primary_key),
        key=sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(column)) for column in primary_key),
    )
    return {tuple(row) for row in conn.execute(statement, (json.dumps(rows),)).fetchall()}


def _fill_deferred(conn: psycopg.Connection, table: str, primary_key: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    assignments = sql.SQL(", ").join(
        sql.SQL("{column} = snapshot.{column}").format(column=sql.Identifier(column)) for column in DEFERRED_COLUMNS[table]
    )
    statement = sql.SQL("UPDATE {table} AS live SET {assignments} FROM {rows} AS snapshot WHERE {same_key}").format(
        table=sql.Identifier(table), assignments=assignments, rows=_snapshot_rows(table), same_key=_same_key(primary_key)
    )
    conn.execute(statement, (json.dumps(rows),))


def _not_tombstoned(columns: list[str]) -> sql.Composable:
    conditions = [
        sql.SQL("NOT (live.{column} IS NOT NULL AND snapshot.{column} IS NULL)").format(column=sql.Identifier(column))
        for column in TOMBSTONE_COLUMNS
        if column in columns
    ]
    return sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("TRUE")


def _overwrite_content(conn: psycopg.Connection, table: str, columns: list[str], primary_key: list[str], rows: list[dict[str, Any]]) -> int:
    assignments = sql.SQL(", ").join(
        (
            sql.SQL("{column} = GREATEST(live.{column}, snapshot.{column})")
            if column in MONOTONIC_COLUMNS
            else sql.SQL("{column} = snapshot.{column}")
        ).format(column=sql.Identifier(column))
        for column in columns
        if column not in primary_key and column not in LIVE_COLUMNS.get(table, ())
    )
    statement = sql.SQL(
        "UPDATE {table} AS live SET {assignments} FROM {rows} AS snapshot "
        "WHERE {same_key} AND to_jsonb(live) IS DISTINCT FROM to_jsonb(snapshot) AND {not_tombstoned}"
    ).format(
        table=sql.Identifier(table),
        assignments=assignments,
        rows=_snapshot_rows(table),
        same_key=_same_key(primary_key),
        not_tombstoned=_not_tombstoned(columns),
    )
    return conn.execute(statement, (json.dumps(rows),)).rowcount


def _differing(conn: psycopg.Connection, table: str, primary_key: list[str], rows: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    statement = sql.SQL(
        "SELECT {key} FROM {table} AS live JOIN {rows} AS snapshot ON {same_key} "
        "WHERE to_jsonb(live) IS DISTINCT FROM to_jsonb(snapshot)"
    ).format(
        key=sql.SQL(", ").join(sql.SQL("live.{}::text").format(sql.Identifier(column)) for column in primary_key),
        table=sql.Identifier(table),
        rows=_snapshot_rows(table),
        same_key=_same_key(primary_key),
    )
    return [tuple(row) for row in conn.execute(statement, (json.dumps(rows),)).fetchall()]


def restore_project(database_url: str, artifact_root: Path, source: Path, *, confirm: str) -> dict[str, Any]:
    """Merge the snapshot into an empty or a live database, stop unfinished commands, and put artifact bytes back.

    Missing rows are inserted. Content rows go back to the snapshot unless they were deleted
    since; their lifecycle and security columns keep the live value. Ledger and lifecycle rows
    that changed after the backup keep their live value, so a task that finished after the
    backup stays finished. Nothing is deleted. Artifact bytes are merged into a staged copy
    first and swapped in just before the database commit, so a failure on either side leaves
    both as they were.
    """
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
    copied = source / "artifacts"
    staging, files = _stage_artifacts(copied, artifact_root) if copied.is_dir() else (None, {"restored_files": 0, "kept_live_files": 0})
    previous = None
    swapped = False
    try:
        with psycopg.connect(database_url) as conn:
            with conn.transaction():
                merged = _merge_tables(conn, snapshot)
                if staging is not None:
                    previous = _swap_in(staging, artifact_root)
                    swapped = True
    except Exception:
        if swapped:
            _swap_back(artifact_root, previous)
        elif staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    if previous is not None:
        shutil.rmtree(previous, ignore_errors=True)
    return {
        "dispatch": "stopped",
        "restored_tables": {table: len(snapshot[table]) for table in RESTORE_ORDER},
        **merged,
        **files,
        "volume_deleted": False,
    }


def _merge_tables(conn: psycopg.Connection, snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    inserted: dict[str, int] = {}
    overwritten: dict[str, int] = {}
    kept_live: dict[str, int] = {}
    kept_tasks: list[str] = []
    _assert_schema_covered(conn)
    shapes = {table: (_columns(conn, table), _primary_key(conn, table)) for table in RESTORE_ORDER}
    for table, (columns, primary_key) in shapes.items():
        if not columns or not primary_key:
            raise RuntimeError(f"restore_table_rejected:{table}")
    added: dict[str, set[tuple[str, ...]]] = {}
    for table in RESTORE_ORDER:
        columns, primary_key = shapes[table]
        added[table] = _insert_missing(conn, table, columns, primary_key, snapshot[table])
        inserted[table] = len(added[table])
    for table in DEFERRED_COLUMNS:
        primary_key = shapes[table][1]
        _fill_deferred(conn, table, primary_key, [row for row in snapshot[table] if _key_of(row, primary_key) in added[table]])
    for table in RESTORE_ORDER:
        columns, primary_key = shapes[table]
        if table in CONTENT_TABLES:
            overwritten[table] = _overwrite_content(conn, table, columns, primary_key, snapshot[table])
        differing = _differing(conn, table, primary_key, snapshot[table])
        if differing:
            kept_live[table] = len(differing)
        if table == "tasks":
            kept_tasks = sorted(key[primary_key.index("id")] for key in differing)
    # Stop after the snapshot write. Restored PENDING rows would be dispatchable again.
    stopped = conn.execute(
        """
        UPDATE command_outbox
        SET status = 'STOPPED'
        WHERE status IN ('PENDING', 'DELIVERING')
        RETURNING id::text
        """
    ).fetchall()
    pending = conn.execute("SELECT count(*) FROM command_outbox WHERE status IN ('PENDING', 'DELIVERING')").fetchone()
    return {
        "stopped_commands": [row[0] for row in stopped],
        "pending_after_restore": int(pending[0]) if pending else 0,
        "inserted_rows": inserted,
        "overwritten_rows": overwritten,
        "kept_live_rows": kept_live,
        "kept_live_tasks": kept_tasks,
    }
