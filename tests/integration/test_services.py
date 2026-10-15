"""Connectivity probes. Missing services are not_run locally and failures in CI."""

from __future__ import annotations

import os
import unittest

import psycopg


def _require(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
        raise AssertionError(f"{name} is required in this run and was not set")
    raise unittest.SkipTest(f"{name} unset; probe not_run")


class ServiceConnectivityTests(unittest.TestCase):
    def test_postgres_and_pgvector(self) -> None:
        url = _require("AETHER_ADMIN_DATABASE_URL")
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('server_version')")
                version = cur.fetchone()[0]
                cur.execute("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
                vector = cur.fetchone()
        self.assertTrue(version)
        self.assertIsNotNone(vector)
        self.assertTrue(str(vector[0]))

    def test_temporal_frontend_health(self) -> None:
        address = _require("AETHER_TEMPORAL_ADDRESS")
        import asyncio
        from temporalio.client import Client

        async def check() -> None:
            client = await Client.connect(address)
            await client.service_client.check_health()

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
