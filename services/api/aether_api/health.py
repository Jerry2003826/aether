"""Liveness and readiness. Readiness probes real dependencies and never invents ok."""

from __future__ import annotations

import os
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse


def _postgres_probe(url: str | None) -> dict[str, Any]:
    if not url:
        return {"status": "not_configured", "probed": False, "version": None}
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('server_version')")
                version = cur.fetchone()[0]
                cur.execute(
                    "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'"
                )
                row = cur.fetchone()
        return {
            "status": "ok",
            "probed": True,
            "version": version,
            "pgvector": None if row is None else row[0],
        }
    except Exception as exc:  # noqa: BLE001 — probe must report the failure, not raise as success
        return {"status": "unavailable", "probed": False, "version": None, "error": type(exc).__name__}


def _temporal_probe(address: str | None) -> dict[str, Any]:
    if not address:
        return {"status": "not_configured", "probed": False, "version": None}
    try:
        from temporalio.client import Client
        import asyncio

        async def _check() -> None:
            client = await Client.connect(address)
            await client.service_client.check_health()

        asyncio.run(_check())
        return {"status": "ok", "probed": True, "address": address, "persistence": "not_asserted_by_health"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "probed": False, "version": None, "error": type(exc).__name__}


def create_app() -> FastAPI:
    app = FastAPI(title="AETHER", version="1.4.0")

    @app.get("/health/live")
    def live() -> dict[str, Any]:
        return {"status": "ok", "dependencies": {}}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        postgres = _postgres_probe(os.environ.get("AETHER_ADMIN_DATABASE_URL") or os.environ.get("AETHER_DATABASE_URL"))
        temporal = _temporal_probe(os.environ.get("AETHER_TEMPORAL_ADDRESS"))
        dependencies = {"postgres": postgres, "temporal": temporal}
        ready_ok = postgres.get("probed") is True and temporal.get("probed") is True
        body = {"status": "ok" if ready_ok else "unavailable", "dependencies": dependencies}
        return JSONResponse(status_code=200 if ready_ok else 503, content=body)

    return app
