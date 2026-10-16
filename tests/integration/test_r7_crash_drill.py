"""SYS-09: backup, crash, restore, then reconcile a live task with Temporal."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from temporalio.client import Client

from aether_storage.bootstrap import ensure_database
from aether_worker.durable_workflow import DurableTaskWorkflow
from scripts.run_crash_recovery_drill import start_drill_temporal, stop_drill_temporal

ROOT = Path(__file__).resolve().parents[2]


def test_crash_drill_reports_divergence_without_rewriting_the_task(admin_url: str) -> None:
    database_name = f"aether_drill_{uuid.uuid4().hex[:12]}"
    ensure_database(admin_url, database=database_name, rotate_passwords=False)
    database_url = _database_url(admin_url, database_name)
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    workflow_id = f"task-{task_id}"
    _seed(database_url, owner_id, project_id, task_id, workflow_id)
    port = _port()
    container = f"aether-drill-{uuid.uuid4().hex[:8]}"
    work = _drill_dir()
    state = work / container
    address = f"127.0.0.1:{port}"
    start_drill_temporal(name=container, port=port, state_dir=state)
    try:
        asyncio.run(_start(address, workflow_id, str(task_id)))
        destination = work / "backup"
        artifacts = work / "artifacts"
        artifacts.mkdir()
        common = [
            "--database-url",
            database_url,
            "--destination",
            str(destination),
            "--temporal-container",
            container,
            "--temporal-db",
            str(state / "temporal.db"),
            "--temporal-address",
            address,
        ]
        backup = _run("backup", [*common, "--artifact-root", str(artifacts), "--workflow-id", workflow_id])
        assert backup.returncode == 0, backup.stdout + backup.stderr
        with psycopg.connect(database_url) as conn:
            conn.execute("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = %s AND status = 'QUEUED'", (task_id,))
            conn.commit()
        crashed = _run("crash", common)
        assert crashed.returncode == 0, crashed.stdout + crashed.stderr
        restored = _run("restore", [*common, "--artifact-root", str(artifacts), "--confirm", "restore-aether"])
        assert restored.returncode == 0, restored.stdout + restored.stderr
        report_path = work / "report.json"
        command = [
            "reconcile",
            "--database-url",
            database_url,
            "--destination",
            str(destination),
            "--temporal-address",
            address,
            "--workflow-id",
            workflow_id,
            "--project-id",
            str(project_id),
            "--task-id",
            str(task_id),
            "--report",
            str(report_path),
        ]
        first = _run(command[0], command[1:])
        assert first.returncode == 0, first.stdout + first.stderr
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["relation"] == "diverged"
        assert report["action"] == "human_review"
        assert report["rewritten"] is False
        assert report["db_status"] == "SUCCEEDED"
        assert report["temporal_status"] == "RUNNING"
        assert "postgresql://" not in report_path.read_text(encoding="utf-8")
        second = _run(command[0], command[1:])
        assert second.returncode == 0, second.stdout + second.stderr
        assert json.loads(report_path.read_text(encoding="utf-8"))["relation"] == "diverged"
        with psycopg.connect(database_url) as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id = %s", (task_id,)).fetchone()
        assert status is not None
        assert status[0] == "SUCCEEDED"
    finally:
        stop_drill_temporal(container)
        _drop(admin_url, database_name)
        shutil.rmtree(work, ignore_errors=True)


def _seed(database_url: str, owner_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID, workflow_id: str) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute("INSERT INTO owners (id, display_name, password_hash) VALUES (%s, '演练', 'x')", (owner_id,))
        conn.execute("INSERT INTO projects (id, owner_id, name) VALUES (%s, %s, '演练')", (project_id, owner_id))
        conn.execute(
            "INSERT INTO tasks (id, project_id, original_goal, workflow_id, status) VALUES (%s, %s, '演练目标', %s, 'QUEUED')",
            (task_id, project_id, workflow_id),
        )
        conn.commit()


async def _start(address: str, workflow_id: str, task_id: str) -> None:
    deadline = time.monotonic() + 40
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = await Client.connect(address)
            await client.start_workflow(DurableTaskWorkflow.run, task_id, id=workflow_id, task_queue="aether-drill")
            return
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.4)
    raise AssertionError(f"演练 Temporal 没有接受工作流: {last}")


def _run(command: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "run_crash_recovery_drill.py"), command, *args], capture_output=True, text=True, check=False)


def _database_url(admin_url: str, database_name: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = database_name
    return psycopg.conninfo.make_conninfo(**info)


def _drill_dir() -> Path:
    parent = Path.cwd() / ".aether-drill-work"
    parent.mkdir(mode=0o700, exist_ok=True)
    root = parent / uuid.uuid4().hex
    root.mkdir(mode=0o700)
    return root


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _drop(admin_url: str, database_name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
