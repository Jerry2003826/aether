"""Long-running worker process. Temporal remains the only durable scheduler."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
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
from aether_worker.goal_effect import WorkerEndpoints, apply_task_control, configure, execute_task, prepare_task, set_control_executor
from aether_worker.memory import apply_memory_commands
from aether_worker.pending_jobs import execute_queued_ingestions
from aether_worker.tool_gateway import dispatch_approved_tools
from aether_worker.project_registry import load_projects, save_projects
from aether_worker.watch_tick import run_watch_tick
from aether_worker.watches import ensure_watch_schedules
from aether_worker.watches_workflow import WatchWorkflow


def make_executors() -> tuple[concurrent.futures.ThreadPoolExecutor, concurrent.futures.ThreadPoolExecutor]:
    """Activity work and control delivery do not share one saturated pool."""
    activity = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="aether-activity")
    control = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="aether-control")
    return activity, control


async def run_control(executor: concurrent.futures.Executor, func, /, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, functools.partial(func, *args))


def index_root_from_env() -> Path:
    return Path(os.environ.get("AETHER_INDEX_ROOT", "data/indexes"))


def route_pending(
    dispatcher: Database,
    worker: Database,
    verifier: Database,
    store: ArtifactStore,
    seen: dict[str, str] | None = None,
) -> None:
    """Apply non-temporal outbox rows. Temporal kinds are left for deliver_pending."""
    remembered = {} if seen is None else seen
    before = dict(remembered)
    grouped = _group_pending(_pending_command_rows(dispatcher), remembered)
    _apply_grouped(grouped, worker, verifier, store)
    root = index_root_from_env()
    if seen is not None and remembered != before:
        save_projects(root, remembered)
    if not _defer_progress():
        _execute_remembered(remembered, worker, store, root)


def _pending_command_rows(dispatcher: Database) -> list[Any]:
    excluded = ", ".join(f"'{kind}'" for kind in TEMPORAL_COMMAND_KINDS)
    with dispatcher.transaction() as conn:
        found = conn.execute(
            text(
                "SELECT project_id::text AS project_id, kind, payload FROM command_outbox "
                f"WHERE status = 'PENDING' AND kind NOT IN ({excluded})"
            )
        ).mappings().all()
    return found


def _group_pending(rows: list[Any], remembered: dict[str, str]) -> dict[str, dict[str, Any]]:
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
        if owner_id:
            remembered[project_id] = owner_id
    return grouped


def _apply_grouped(
    grouped: dict[str, dict[str, Any]],
    worker: Database,
    verifier: Database,
    store: ArtifactStore,
) -> None:
    for project_id, bucket in grouped.items():
        owner_id = str(bucket["owner_id"])
        kinds: set[str] = bucket["kinds"]
        try:
            if kinds & set(CONSOLE_KINDS):
                apply_console_commands(worker, owner_id=owner_id, project_id=project_id, store=store)
            if kinds & set(VERIFIER_KINDS):
                apply_verifier_commands(verifier, owner_id=owner_id, project_id=project_id)
            if any(kind.startswith("memory.") for kind in kinds):
                apply_memory_commands(worker, store, owner_id=owner_id, project_id=project_id)
        except Exception as exc:
            print(f"aether-route-failed project={project_id} error={type(exc).__name__}", flush=True)


def _execute_remembered(remembered: dict[str, str], worker: Database, store: ArtifactStore, root: Path) -> None:
    for project_id, owner_id in remembered.items():
        try:
            execute_queued_ingestions(worker, store, owner_id=owner_id, project_id=project_id, index_root=root)
        except Exception as exc:
            print(f"aether-progress-failed project={project_id} error={type(exc).__name__}", flush=True)
        try:
            dispatch_approved_tools(worker, owner_id=owner_id, project_id=project_id)
        except Exception as exc:
            print(f"aether-tool-failed project={project_id} error={type(exc).__name__}", flush=True)


def _defer_progress() -> bool:
    """Test switch. A delivered command can stay QUEUED until a later process resumes it."""
    return os.environ.get("AETHER_DEFER_PROGRESS", "").strip() == "1"


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


def _research_provider() -> str:
    raw = os.environ.get("AETHER_RESEARCH_PROVIDER", "live").strip().lower()
    if raw in {"", "live"}:
        return "live"
    if raw == "scripted":
        return "scripted"
    raise SystemExit("AETHER_RESEARCH_PROVIDER 只能是 live 或 scripted")


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
        research_provider=_research_provider(),
        live_model_base=os.environ.get("AETHER_LIVE_MODEL_BASE", "").strip(),
        live_model_alias=os.environ.get("AETHER_LIVE_MODEL_ALIAS", "").strip(),
        live_model_key=os.environ.get("AETHER_LIVE_MODEL_KEY", ""),
    )


async def serve(endpoints: WorkerEndpoints | None = None) -> None:
    resolved = endpoints or endpoints_from_env()
    configure(resolved)
    activity_executor, control_executor = make_executors()
    try:
        client = await Client.connect(resolved.temporal_address)
    except Exception:
        activity_executor.shutdown(wait=False, cancel_futures=False)
        control_executor.shutdown(wait=False, cancel_futures=False)
        raise
    loop = asyncio.get_running_loop()
    loop.set_default_executor(activity_executor)
    set_control_executor(control_executor)
    worker = Worker(
        client,
        task_queue=resolved.task_queue,
        workflows=[DurableTaskWorkflow, WatchWorkflow],
        activities=[prepare_task, execute_task, apply_task_control, run_watch_tick],
    )
    dispatcher = Database(resolved.dispatcher_url)
    database = Database(resolved.worker_url)
    verifier = Database(resolved.verifier_url)
    store = ArtifactStore(resolved.artifact_root, database)
    seen = load_projects(index_root_from_env())
    stop = asyncio.Event()
    for signame in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signame, stop.set)
    try:
        async with worker:
            print("aether-worker-ready", flush=True)
            while not stop.is_set():
                try:
                    await deliver_pending(
                        dispatcher,
                        client,
                        task_queue=resolved.task_queue,
                        workflow=DurableTaskWorkflow,
                        intake_pause_seconds=resolved.intake_pause_seconds,
                    )
                except Exception as exc:
                    print(f"aether-deliver-failed error={type(exc).__name__}", flush=True)
                try:
                    await run_control(control_executor, route_pending, dispatcher, database, verifier, store, seen)
                except Exception as exc:
                    print(f"aether-route-failed error={type(exc).__name__}", flush=True)
                try:
                    await ensure_watch_schedules(client, database, seen, resolved.task_queue)
                except Exception as exc:
                    print(f"aether-watch-schedule-failed error={type(exc).__name__}", flush=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    continue
    finally:
        activity_executor.shutdown(wait=False, cancel_futures=False)
        control_executor.shutdown(wait=False, cancel_futures=False)
