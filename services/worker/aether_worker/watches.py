"""Project watches. A schedule starts only after the owner approves source, frequency, and budget."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.service import RPCError, RPCStatusCode

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.watches_workflow import WatchWorkflow

TASK_QUEUE = "aether-watches"


class WatchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def create_watch(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    purpose: str,
    source_ids: list[str],
    every_seconds: int,
    budget_tokens: int,
) -> dict[str, Any]:
    watch_id = str(uuid.uuid4())
    schedule_id = f"aether-watch-{watch_id}"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO watches (id, project_id, purpose, schedule_id, source_ids, schedule_spec, budget_spec, status) "
                "VALUES (:id, :project_id, :purpose, :schedule_id, CAST(:sources AS jsonb), CAST(:schedule AS jsonb), CAST(:budget AS jsonb), 'DRAFT')"
            ),
            {
                "id": watch_id,
                "project_id": project_id,
                "purpose": purpose,
                "schedule_id": schedule_id,
                "sources": json.dumps(source_ids),
                "schedule": json.dumps({"every_seconds": every_seconds}),
                "budget": json.dumps({"token_budget": budget_tokens}),
            },
        )
    return {"id": watch_id, "schedule_id": schedule_id, "status": "DRAFT"}


def _load(conn: Any, project_id: str, watch_id: str) -> dict[str, Any]:
    row = conn.execute(
        text(
            "SELECT id::text AS id, schedule_id, source_ids, schedule_spec, budget_spec, status, "
            "last_watermark, approved_by::text AS approved_by "
            "FROM watches WHERE project_id = :project_id AND id = :id FOR UPDATE"
        ),
        {"project_id": project_id, "id": watch_id},
    ).mappings().first()
    if row is None:
        raise WatchError("watch_missing")
    loaded = dict(row)
    for key in ("source_ids", "schedule_spec", "budget_spec", "last_watermark"):
        if isinstance(loaded[key], str):
            loaded[key] = json.loads(loaded[key])
    return loaded


async def enable_watch(
    database: Database,
    client: Client,
    *,
    owner_id: str,
    project_id: str,
    watch_id: str,
    source_ids: list[str],
    every_seconds: int,
    budget_tokens: int,
) -> dict[str, Any]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        watch = _load(conn, project_id, watch_id)
        if watch["status"] != "DRAFT":
            raise WatchError("already_enabled")
        approved = (
            set(watch["source_ids"]) == set(source_ids)
            and int(watch["schedule_spec"]["every_seconds"]) == int(every_seconds)
            and int(watch["budget_spec"]["token_budget"]) == int(budget_tokens)
        )
        if not approved:
            raise WatchError("approval_rejected")
        schedule_id = str(watch["schedule_id"])
    try:
        await client.create_schedule(
            schedule_id,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    WatchWorkflow.run,
                    watch_id,
                    id=f"{schedule_id}-tick",
                    task_queue=TASK_QUEUE,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=int(every_seconds)))]),
            ),
        )
    except ScheduleAlreadyRunningError as exc:
        raise WatchError("schedule_exists") from exc
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE watches SET status = 'ACTIVE', approved_by = :owner_id, version = version + 1 "
                "WHERE project_id = :project_id AND id = :id AND status = 'DRAFT'"
            ),
            {"owner_id": owner_id, "project_id": project_id, "id": watch_id},
        )
    return {"id": watch_id, "schedule_id": schedule_id, "status": "ACTIVE", "external_sent": False}


async def ensure_watch_schedules(
    client: Client,
    database: Database,
    projects: dict[str, str],
    task_queue: str,
) -> None:
    """Create a missing Temporal schedule for each ACTIVE watch on this worker's queue."""
    for project_id, owner_id in projects.items():
        for watch in _active_watches(database, owner_id=owner_id, project_id=project_id):
            await _ensure_watch_schedule(client, watch, project_id=project_id, owner_id=owner_id, task_queue=task_queue)
        for watch in _stopped_watches(database, owner_id=owner_id, project_id=project_id):
            if await _delete_schedule(client, str(watch["schedule_id"])):
                _mark_schedule_removed(
                    database,
                    owner_id=owner_id,
                    project_id=project_id,
                    watch_id=str(watch["id"]),
                    version=int(watch["version"]),
                )


def _active_watches(database: Database, *, owner_id: str, project_id: str) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT id::text AS id, schedule_id, schedule_spec FROM watches "
                "WHERE project_id = :project_id AND status = 'ACTIVE' ORDER BY id"
            ),
            {"project_id": project_id},
        ).mappings().all()
    loaded: list[dict[str, Any]] = []
    for row in rows:
        spec = row["schedule_spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        loaded.append(
            {
                "id": str(row["id"]),
                "schedule_id": str(row["schedule_id"]),
                "every_seconds": _every_seconds(spec),
            }
        )
    return loaded


def _stopped_watches(database: Database, *, owner_id: str, project_id: str) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT id::text AS id, schedule_id, schedule_spec, version FROM watches "
                "WHERE project_id = :project_id AND status IN ('PAUSED', 'DELETED') ORDER BY id"
            ),
            {"project_id": project_id},
        ).mappings().all()
    pending: list[dict[str, Any]] = []
    for row in rows:
        spec = row["schedule_spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        if isinstance(spec, dict) and spec.get("schedule_removed") is True:
            continue
        pending.append({"id": str(row["id"]), "schedule_id": str(row["schedule_id"]), "version": int(row["version"])})
    return pending


def _mark_schedule_removed(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    watch_id: str,
    version: int,
) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE watches SET schedule_spec = jsonb_set(schedule_spec, '{schedule_removed}', 'true'::jsonb, true) "
                "WHERE project_id = :project_id AND id = :id AND version = :version AND status IN ('PAUSED', 'DELETED')"
            ),
            {"project_id": project_id, "id": watch_id, "version": version},
        )


async def _delete_schedule(client: Client, schedule_id: str) -> bool:
    """Return true only after Temporal no longer has this schedule."""
    if await _schedule_missing(client, schedule_id):
        return True
    try:
        await client.get_schedule_handle(schedule_id).delete()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    return True


def _every_seconds(spec: Any) -> int | None:
    if not isinstance(spec, dict):
        return None
    raw = spec.get("interval_seconds", spec.get("every_seconds"))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


async def _ensure_watch_schedule(
    client: Client,
    watch: dict[str, Any],
    *,
    project_id: str,
    owner_id: str,
    task_queue: str,
) -> None:
    every = watch["every_seconds"]
    if every is None:
        print(f"aether-watch-schedule-skipped watch={watch['id']}", flush=True)
        return
    schedule_id = str(watch["schedule_id"])
    if not await _schedule_missing(client, schedule_id):
        return
    try:
        await client.create_schedule(
            schedule_id,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    WatchWorkflow.run,
                    args=[str(watch["id"]), project_id, owner_id],
                    id=f"{schedule_id}-tick",
                    task_queue=task_queue,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=int(every)))]),
            ),
            trigger_immediately=True,
        )
    except ScheduleAlreadyRunningError:
        return


async def _schedule_missing(client: Client, schedule_id: str) -> bool:
    try:
        await client.get_schedule_handle(schedule_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return True
        raise
    return False


async def stop_watch(database: Database, client: Client, *, owner_id: str, project_id: str, watch_id: str) -> dict[str, Any]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        watch = _load(conn, project_id, watch_id)
        schedule_id = str(watch["schedule_id"])
    handle = client.get_schedule_handle(schedule_id)
    await handle.delete()
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE watches SET status = 'PAUSED', version = version + 1, "
                "schedule_spec = jsonb_set(schedule_spec, '{schedule_removed}', 'true'::jsonb, true) "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": watch_id},
        )
    return {"id": watch_id, "status": "PAUSED"}


def trigger_watch(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    watch_id: str,
    watermark: str,
    trigger_key: str,
    store: ArtifactStore | None = None,
) -> dict[str, Any]:
    """Record one changed watch run.

    With ``store``, the watch-run artifact row is inserted on the same connection, so
    the notification, ``last_run_at`` and the artifact commit or roll back together.
    A failed write leaves no notification behind, and a retry with the same trigger
    key runs again instead of returning ``duplicate``.
    """
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        watch = _load(conn, project_id, watch_id)
        if watch["status"] != "ACTIVE":
            return {"dispatched": False, "summarized": False, "reason": "stopped", "notification_id": None, "external_sent": False}
        if not watch["approved_by"]:
            return {"dispatched": False, "summarized": False, "reason": "unapproved", "notification_id": None, "external_sent": False}
        previous = None if watch["last_watermark"] is None else str(watch["last_watermark"].get("value"))
        if previous == watermark:
            return {"dispatched": False, "summarized": False, "reason": "unchanged", "notification_id": None, "external_sent": False}
        content = {"summary": f"水位变为 {watermark}", "external_sent": False}
        notification_id = conn.execute(
            text(
                "INSERT INTO notifications (project_id, watch_id, dedup_key, title, content) "
                "VALUES (:project_id, :watch_id, :dedup_key, :title, CAST(:content AS jsonb)) "
                "ON CONFLICT (project_id, dedup_key) DO NOTHING RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "watch_id": watch_id,
                "dedup_key": trigger_key,
                "title": "跟踪有变化",
                "content": json.dumps(content, ensure_ascii=False),
            },
        ).scalar()
        if notification_id is None:
            return {"dispatched": False, "summarized": False, "reason": "duplicate", "notification_id": None, "external_sent": False}
        conn.execute(
            text(
                "UPDATE watches SET last_watermark = CAST(:watermark AS jsonb), last_run_at = now() "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"watermark": json.dumps({"value": watermark}), "project_id": project_id, "id": watch_id},
        )
        if store is not None:
            # put() writes the blob to disk before inserting the row on this connection.
            # If the transaction rolls back, the blob stays unreferenced; storage is
            # content-addressed (project/sha256) and the journal records rolled_back,
            # so the retry reuses the same blob and recovery does not resurrect it.
            store.put(
                owner_id=owner_id,
                project_id=project_id,
                data=f"watch-run\n{watch_id}\n".encode(),
                media_type="text/plain",
                conn=conn,
            )
    return {
        "dispatched": True,
        "summarized": True,
        "reason": "changed",
        "notification_id": str(notification_id),
        "external_sent": False,
    }
