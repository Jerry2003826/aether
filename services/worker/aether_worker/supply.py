"""Lockfile SBOM, backup, and restore. Restore does not delete volumes."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import psycopg

SBOM_TABLES = ("projects", "artifacts", "tasks", "command_outbox")


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


def restore_project(database_url: str, artifact_root: Path, source: Path, *, confirm: str) -> dict[str, Any]:
    """Stop dispatch and put artifact bytes back. Do not delete volumes or mark tasks succeeded."""
    if confirm != "restore-aether":
        raise RuntimeError("restore_not_confirmed")
    recorded = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((source / "tables.json").read_bytes()).hexdigest()
    if digest != recorded["sha256"]:
        raise RuntimeError("backup_hash_mismatch")
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            stopped = conn.execute(
                "UPDATE command_outbox SET status = 'FAILED' WHERE status IN ('PENDING', 'DELIVERING') RETURNING id::text"
            ).fetchall()
            pending = conn.execute(
                "SELECT count(*) FROM command_outbox WHERE status IN ('PENDING', 'DELIVERING')"
            ).fetchone()
    copied = source / "artifacts"
    if copied.is_dir():
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
        shutil.copytree(copied, artifact_root)
    return {
        "dispatch": "stopped",
        "stopped_commands": [row[0] for row in stopped],
        "pending_after_restore": int(pending[0]) if pending else 0,
        "volume_deleted": False,
    }
