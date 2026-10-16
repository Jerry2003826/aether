"""One watch tick. The workflow schedules this activity and does no I/O itself."""

from __future__ import annotations

import asyncio

from temporalio import activity

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker import goal_effect


@activity.defn
async def run_watch_tick(payload: dict) -> None:
    endpoints = goal_effect.ENDPOINTS
    if endpoints is None:
        raise RuntimeError("worker endpoints are not configured")
    trigger_key = activity.info().workflow_run_id
    await asyncio.to_thread(_tick, endpoints, payload, trigger_key)


def _tick(endpoints: goal_effect.WorkerEndpoints, payload: dict, trigger_key: str) -> None:
    # pending_jobs imports watches, and watches imports WatchWorkflow, which imports this module.
    from aether_worker.pending_jobs import finish_watch_tick

    database = Database(endpoints.worker_url)
    store = ArtifactStore(endpoints.artifact_root, database)
    finish_watch_tick(
        database,
        store,
        owner_id=str(payload["owner_id"]),
        project_id=str(payload["project_id"]),
        watch_id=str(payload["watch_id"]),
        trigger_key=trigger_key,
    )
