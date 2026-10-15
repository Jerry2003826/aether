"""Liveness and readiness. Readiness probes real dependencies and never invents ok."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import psycopg
from fastapi.responses import JSONResponse
from temporalio.client import Client


def postgres_probe(url: str | None) -> dict[str, Any]:
    if not url:
        return {"status": "not_configured", "probed": False, "version": None}
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('server_version')")
                version = cur.fetchone()[0]
                cur.execute("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
                row = cur.fetchone()
        return {"status": "ok", "probed": True, "version": version, "pgvector": None if row is None else row[0]}
    except Exception as exc:  # noqa: BLE001 — the probe reports failure instead of pretending to be ready
        return {"status": "unavailable", "probed": False, "version": None, "error": type(exc).__name__}


def temporal_probe(address: str | None) -> dict[str, Any]:
    if not address:
        return {"status": "not_configured", "probed": False, "version": None}

    async def _check() -> None:
        client = await Client.connect(address)
        await client.service_client.check_health()

    try:
        asyncio.run(_check())
        return {"status": "ok", "probed": True, "address": address, "persistence": "not_asserted_by_health"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "probed": False, "version": None, "error": type(exc).__name__}


def ready_payload() -> JSONResponse:
    postgres = postgres_probe(os.environ.get("AETHER_ADMIN_DATABASE_URL") or os.environ.get("AETHER_DATABASE_URL"))
    temporal = temporal_probe(os.environ.get("AETHER_TEMPORAL_ADDRESS"))
    ready_ok = postgres.get("probed") is True and temporal.get("probed") is True
    body = {"status": "ok" if ready_ok else "unavailable", "dependencies": {"postgres": postgres, "temporal": temporal}}
    return JSONResponse(status_code=200 if ready_ok else 503, content=body)
