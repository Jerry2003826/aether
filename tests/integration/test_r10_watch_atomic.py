"""A watch run commits its notification, last_run_at and watch-run artifact together."""

from __future__ import annotations

import asyncio

import pytest
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.pending_jobs import finish_watch_tick
from aether_worker.watches import create_watch, enable_watch
from sqlalchemy import text
from temporalio.service import RPCError

from tests.integration.test_watches import _count, _session, _temporal


class _FailingOncePut(ArtifactStore):
    """The first put raises, as a full disk or a crash before the artifact row would."""

    def __init__(self, root, database) -> None:
        super().__init__(root, database)
        self.failed = False

    def put(self, **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("artifact write failed")
        return super().put(**kwargs)


class _ObservingPut(ArtifactStore):
    """Reads last_run_at over a separate connection when the artifact is written."""

    def __init__(self, root, database, observer: Database, owner_id: str, project_id: str, watch_id: str) -> None:
        super().__init__(root, database)
        self.observer = observer
        self.owner_id = owner_id
        self.project_id = project_id
        self.watch_id = watch_id
        self.seen: list[object] = []

    def put(self, **kwargs):
        with self.observer.transaction(owner_id=self.owner_id, project_id=self.project_id) as conn:
            self.seen.append(
                conn.execute(
                    text("SELECT last_run_at FROM watches WHERE project_id = :project_id AND id = :id"),
                    {"project_id": self.project_id, "id": self.watch_id},
                ).scalar_one()
            )
        return super().put(**kwargs)


def _active_watch(client, role_urls, key: str):
    owner_id, project_id = _session(client, key)
    database = Database(role_urls["aether_worker"])
    temporal = _temporal()
    created = create_watch(
        database,
        owner_id=owner_id,
        project_id=project_id,
        purpose="跟踪原子性",
        source_ids=["source-notes"],
        every_seconds=3600,
        budget_tokens=20,
    )
    asyncio.run(
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
    return database, temporal, owner_id, project_id, created


def _delete_schedule(temporal, schedule_id: str) -> None:
    try:
        asyncio.run(temporal.get_schedule_handle(schedule_id).delete())
    except RPCError:
        pass


def _watch_run_artifacts(database: Database, store: ArtifactStore, owner_id: str, project_id: str, watch_id: str) -> int:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        ids = conn.execute(
            text("SELECT id::text FROM artifacts WHERE project_id = :project_id AND deleted_at IS NULL"),
            {"project_id": project_id},
        ).scalars().all()
    found = 0
    for artifact_id in ids:
        data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
        if data == f"watch-run\n{watch_id}\n".encode():
            found += 1
    return found


def _last_run_at(database: Database, owner_id: str, project_id: str, watch_id: str):
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return conn.execute(
            text("SELECT last_run_at FROM watches WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": watch_id},
        ).scalar_one()


def test_a_failed_artifact_write_rolls_back_the_run_so_the_retry_writes_it(client, role_urls) -> None:
    database, temporal, owner_id, project_id, created = _active_watch(client, role_urls, "watch-atomic-retry")
    root = client.app.state.settings.artifact_root
    store = _FailingOncePut(root, database)
    try:
        with pytest.raises(RuntimeError, match="artifact write failed"):
            finish_watch_tick(database, store, owner_id=owner_id, project_id=project_id, watch_id=created["id"], trigger_key="tick-retry")
        assert _count(database, owner_id, project_id, created["id"]) == 0, "the notification must roll back with the failed artifact write"
        assert _last_run_at(database, owner_id, project_id, created["id"]) is None, "last_run_at must roll back with the failed artifact write"
        finish_watch_tick(database, store, owner_id=owner_id, project_id=project_id, watch_id=created["id"], trigger_key="tick-retry")
        assert _watch_run_artifacts(database, store, owner_id, project_id, created["id"]) == 1, "the retry with the same trigger key must write the watch-run artifact"
        assert _count(database, owner_id, project_id, created["id"]) == 1, "the retry must leave exactly one notification"
        assert _last_run_at(database, owner_id, project_id, created["id"]) is not None
    finally:
        _delete_schedule(temporal, created["schedule_id"])


def test_last_run_at_is_not_visible_before_the_artifact_is_written(client, role_urls) -> None:
    database, temporal, owner_id, project_id, created = _active_watch(client, role_urls, "watch-atomic-order")
    root = client.app.state.settings.artifact_root
    store = _ObservingPut(root, database, Database(role_urls["aether_worker"]), owner_id, project_id, created["id"])
    try:
        finish_watch_tick(database, store, owner_id=owner_id, project_id=project_id, watch_id=created["id"], trigger_key="tick-order")
        assert store.seen == [None], f"last_run_at was already committed when the artifact was written: {store.seen}"
        assert _watch_run_artifacts(database, store, owner_id, project_id, created["id"]) == 1
        assert _last_run_at(database, owner_id, project_id, created["id"]) is not None
        assert _count(database, owner_id, project_id, created["id"]) == 1
    finally:
        _delete_schedule(temporal, created["schedule_id"])
