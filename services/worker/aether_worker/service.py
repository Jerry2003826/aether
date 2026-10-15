"""Long-running worker process. Temporal remains the only durable scheduler."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.console_commands import CONSOLE_KINDS, VERIFIER_KINDS, apply_console_commands, apply_verifier_commands
from aether_worker.dispatcher import TEMPORAL_COMMAND_KINDS, deliver_pending
from aether_worker.durable_workflow import DurableTaskWorkflow
from aether_worker.goal_effect import WorkerEndpoints, configure, execute_task, prepare_task
from aether_worker.memory import apply_memory_commands


def route_pending(dispatcher: Database, worker: Database, verifier: Database, store: ArtifactStore) -> None:
    """Apply non-temporal outbox rows. Temporal kinds are left for deliver_pending."""
    excluded = ", ".join(f"'{kind}'" for kind in TEMPORAL_COMMAND_KINDS)
    with dispatcher.transaction() as conn:
        rows = conn.execute(
            text(
                "SELECT project_id::text AS project_id, kind, payload FROM command_outbox "
                f"WHERE status = 'PENDING' AND kind NOT IN ({excluded})"
            )
        ).mappings().all()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        project_id = str(row["project_id"])
        bucket = grouped.setdefault(project_id, {"owner_id": "", "kinds": set()})
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        if not bucket["owner_id"] and payload.get("owner_id"):
            bucket["owner_id"] = str(payload["owner_id"])
        bucket["kinds"].add(str(row["kind"]))
    for project_id, bucket in grouped.items():
        owner_id = str(bucket["owner_id"])
        kinds: set[str] = bucket["kinds"]
        if kinds & set(CONSOLE_KINDS):
            apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
        if kinds & set(VERIFIER_KINDS):
            apply_verifier_commands(verifier, owner_id=owner_id, project_id=project_id)
        if any(kind.startswith("memory.") for kind in kinds):
            apply_memory_commands(worker, store, owner_id=owner_id, project_id=project_id)


def _intake_pause_seconds() -> float:
    raw = os.environ.get("AETHER_INTAKE_PAUSE_SECONDS", "0").strip()
    if raw == "":
        return 0.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit("AETHER_INTAKE_PAUSE_SECONDS 必须是非负秒数") from exc
    if value < 0:
        raise SystemExit("AETHER_INTAKE_PAUSE_SECONDS 必须是非负秒数")
    return value


def endpoints_from_env() -> WorkerEndpoints:
    missing = [
        name
        for name in ("AETHER_WORKER_DATABASE_URL", "AETHER_VERIFIER_DATABASE_URL", "AETHER_DISPATCHER_DATABASE_URL", "AETHER_TEMPORAL_ADDRESS")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit("worker 缺少环境变量: " + ", ".join(missing))
    return WorkerEndpoints(
        worker_url=os.environ["AETHER_WORKER_DATABASE_URL"],
        verifier_url=os.environ["AETHER_VERIFIER_DATABASE_URL"],
        dispatcher_url=os.environ["AETHER_DISPATCHER_DATABASE_URL"],
        artifact_root=Path(os.environ.get("AETHER_ARTIFACT_ROOT", "data/artifacts")),
        temporal_address=os.environ["AETHER_TEMPORAL_ADDRESS"],
        task_queue=os.environ.get("AETHER_TASK_QUEUE", "aether-tasks"),
        intake_pause_seconds=_intake_pause_seconds(),
    )


async def serve(endpoints: WorkerEndpoints | None = None) -> None:
    resolved = endpoints or endpoints_from_env()
    configure(resolved)
    client = await Client.connect(resolved.temporal_address)
    worker = Worker(
        client,
        task_queue=resolved.task_queue,
        workflows=[DurableTaskWorkflow],
        activities=[prepare_task, execute_task],
    )
    dispatcher = Database(resolved.dispatcher_url)
    database = Database(resolved.worker_url)
    verifier = Database(resolved.verifier_url)
    store = ArtifactStore(resolved.artifact_root, database)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signame, stop.set)
    async with worker:
        print("aether-worker-ready", flush=True)
        while not stop.is_set():
            await deliver_pending(
                dispatcher,
                client,
                task_queue=resolved.task_queue,
                workflow=DurableTaskWorkflow,
                intake_pause_seconds=resolved.intake_pause_seconds,
            )
            route_pending(dispatcher, database, verifier, store)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                continue
