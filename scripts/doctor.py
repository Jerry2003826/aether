"""Report installed, configured and probed state. Null is never treated as locked."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "domain"))

from aether_domain.locks import lock_state  # noqa: E402


def _component(name: str, *, installed: bool, configured: bool, probed: bool | None, version: object, detail: str) -> dict:
    return {
        "name": name,
        "installed": installed,
        "configured": configured,
        "probed": probed,
        "version": version,
        "lock_state": lock_state(version),
        "detail": detail,
    }


def postgres_probe(url: str | None) -> dict:
    if not url:
        return _component("postgres", installed=shutil.which("psql") is not None, configured=False, probed=False, version=None, detail="AETHER_ADMIN_DATABASE_URL unset")
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                cur.execute("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
                row = cur.fetchone()
        vector = None if row is None else row[0]
        detail = f"pgvector={vector}" if vector else "pgvector extension not available"
        return _component("postgres", installed=True, configured=True, probed=True, version=version, detail=detail)
    except Exception as exc:  # noqa: BLE001
        return _component("postgres", installed=shutil.which("psql") is not None, configured=True, probed=False, version=None, detail=f"{type(exc).__name__}: connection failed")


def _temporal_cli_version() -> str | None:
    cli = shutil.which("temporal")
    if cli is None:
        return None
    import subprocess

    completed = subprocess.run([cli, "--version"], text=True, capture_output=True, check=False)
    line = (completed.stdout or completed.stderr).strip().splitlines()
    if completed.returncode != 0 or not line:
        return None
    # Example: "temporal version 1.9.1 (Server 1.32.0, UI 2.54.1)"
    parts = line[0].split()
    return parts[2] if len(parts) >= 3 and parts[0] == "temporal" else None


def temporal_probe(address: str | None) -> dict:
    cli_version = _temporal_cli_version()
    if not address:
        return _component(
            "temporal",
            installed=cli_version is not None,
            configured=False,
            probed=False,
            version=None,
            detail="AETHER_TEMPORAL_ADDRESS unset; dev server is not release persistence",
        )
    try:
        import asyncio
        from temporalio.client import Client

        async def _check() -> None:
            client = await Client.connect(address)
            await client.service_client.check_health()

        asyncio.run(_check())
        return _component(
            "temporal",
            installed=True,
            configured=True,
            probed=True,
            version=cli_version,
            detail="CLI and frontend health probed; start-dev sqlite is not release persistence",
        )
    except Exception as exc:  # noqa: BLE001
        return _component("temporal", installed=cli_version is not None, configured=True, probed=False, version=None, detail=f"{type(exc).__name__}: connection failed")


def build_report() -> dict:
    py_version = platform.python_version()
    python_ok = sys.version_info >= (3, 13)
    uv_lock = ROOT / "uv.lock"
    pnpm_lock = ROOT / "apps" / "web" / "pnpm-lock.yaml"
    node = shutil.which("node")
    components = [
        _component(
            "python",
            installed=True,
            configured=python_ok,
            probed=True,
            version=py_version if python_ok else None,
            detail=f"executable={sys.executable}",
        ),
        _component(
            "uv_lock",
            installed=(ROOT / "pyproject.toml").is_file(),
            configured=uv_lock.is_file(),
            probed=uv_lock.is_file(),
            version="uv.lock" if uv_lock.is_file() else None,
            detail="hashes come from uv resolution",
        ),
        _component(
            "pnpm_lock",
            installed=(ROOT / "apps" / "web" / "package.json").is_file(),
            configured=pnpm_lock.is_file(),
            probed=pnpm_lock.is_file(),
            version="pnpm-lock.yaml" if pnpm_lock.is_file() else None,
            detail=f"node_on_path={node}",
        ),
        postgres_probe(os.environ.get("AETHER_ADMIN_DATABASE_URL") or os.environ.get("AETHER_DATABASE_URL")),
        temporal_probe(os.environ.get("AETHER_TEMPORAL_ADDRESS")),
        _component("docker", installed=shutil.which("docker") is not None, configured=False, probed=False, version=None, detail="compose runtime not probed on this host"),
        _component("openhands", installed=False, configured=False, probed=None, version=None, detail="see reports/f01-openhands.json when the probe has run"),
        _component("bge_m3", installed=False, configured=False, probed=None, version=None, detail="weights are not locked until a local revision probe succeeds"),
    ]
    return {
        "spec_version": "1.4",
        "doctor": "aether",
        "null_is_locked": False,
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-services", action="store_true")
    parser.add_argument("--write", type=Path, default=ROOT / "reports" / "doctor.json")
    args = parser.parse_args()
    report = build_report()
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not any(item["name"] == "python" and item["configured"] for item in report["components"]):
        return 1
    if args.require_services:
        required = {item["name"]: item for item in report["components"]}
        for name in ("postgres", "temporal"):
            if required[name]["probed"] is not True or required[name]["lock_state"] == "unlocked" and name == "postgres" and required[name]["version"] is None:
                return 1
            if required[name]["probed"] is not True:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
