import asyncio
import os

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.service import RPCError

from aether_storage.db import Database
from aether_worker.watches import WatchError, create_watch, enable_watch, stop_watch, trigger_watch
from tests.integration.test_auth import ORIGIN, _bootstrap


def _session(client, key: str):
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    headers = {**ORIGIN, "X-CSRF-Token": body["csrf_token"], "Idempotency-Key": key}
    project = client.post("/v1/projects", headers=headers, json={"name": "跟踪"})
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"]


def _temporal() -> Client:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    return asyncio.run(Client.connect(address))


def _count(database: Database, owner_id: str, project_id: str, watch_id: str) -> int:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM notifications WHERE project_id = :project_id AND watch_id = :watch_id"),
                {"project_id": project_id, "watch_id": watch_id},
            ).scalar_one()
        )


def test_watch_requires_approval_skips_unchanged_and_does_not_send_out(client, role_urls) -> None:
    owner_id, project_id = _session(client, "watch-project")
    database = Database(role_urls["aether_worker"])
    temporal = _temporal()
    created = create_watch(
        database,
        owner_id=owner_id,
        project_id=project_id,
        purpose="跟踪发布说明",
        source_ids=["source-notes"],
        every_seconds=3600,
        budget_tokens=20,
    )
    with pytest.raises(RPCError):
        asyncio.run(temporal.get_schedule_handle(created["schedule_id"]).describe())
    before = trigger_watch(
        database,
        owner_id=owner_id,
        project_id=project_id,
        watch_id=created["id"],
        watermark="v1",
        trigger_key="tick-1",
    )
    assert before["dispatched"] is False
    assert before["reason"] == "stopped"
    with pytest.raises(WatchError) as rejected:
        asyncio.run(
            enable_watch(
                database,
                temporal,
                owner_id=owner_id,
                project_id=project_id,
                watch_id=created["id"],
                source_ids=["source-notes"],
                every_seconds=60,
                budget_tokens=20,
            )
        )
    assert rejected.value.code == "approval_rejected"
    enabled = asyncio.run(
        enable_watch(
            database,
            temporal,
            owner_id=owner_id,
            project_id=project_id,
            watch_id=created["id"],
            source_ids=["source-notes"],
            every_seconds=3600,
            budget_tokens=20,
        )
    )
    assert enabled["status"] == "ACTIVE"
    assert enabled["external_sent"] is False
    described = asyncio.run(temporal.get_schedule_handle(created["schedule_id"]).describe())
    assert described.id == created["schedule_id"]
    try:
        changed = trigger_watch(
            database,
            owner_id=owner_id,
            project_id=project_id,
            watch_id=created["id"],
            watermark="v1",
            trigger_key="tick-1",
        )
        assert changed["summarized"] is True
        assert changed["external_sent"] is False
        unchanged = trigger_watch(
            database,
            owner_id=owner_id,
            project_id=project_id,
            watch_id=created["id"],
            watermark="v1",
            trigger_key="tick-2",
        )
        assert unchanged["reason"] == "unchanged"
        assert unchanged["summarized"] is False
        duplicate = trigger_watch(
            database,
            owner_id=owner_id,
            project_id=project_id,
            watch_id=created["id"],
            watermark="v2",
            trigger_key="tick-1",
        )
        assert duplicate["reason"] == "duplicate"
        assert _count(database, owner_id, project_id, created["id"]) == 1
        stopped = asyncio.run(stop_watch(database, temporal, owner_id=owner_id, project_id=project_id, watch_id=created["id"]))
        assert stopped["status"] == "PAUSED"
        later = trigger_watch(
            database,
            owner_id=owner_id,
            project_id=project_id,
            watch_id=created["id"],
            watermark="v3",
            trigger_key="tick-3",
        )
        assert later["reason"] == "stopped"
        assert later["dispatched"] is False
        assert _count(database, owner_id, project_id, created["id"]) == 1
    finally:
        handle = temporal.get_schedule_handle(created["schedule_id"])
        try:
            asyncio.run(handle.delete())
        except RPCError:
            pass
