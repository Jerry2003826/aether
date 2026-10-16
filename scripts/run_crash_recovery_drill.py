"""Repeatable crash drill: backup Postgres and Temporal, crash, restore, reconcile.

The Temporal container must be an aether-drill-* process with a sqlite file.
This script does not stop the shared development server.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import psycopg
from temporalio.client import Client

from aether_worker.crash_drill import (
    accepted_workflow_id,
    assert_crash_target,
    confine,
    copy_temporal_files,
    reconcile_observation,
    remove_temporal_files,
    write_marker,
    write_report,
    write_temporal_snapshot,
)
from aether_worker.supply import backup_project, restore_project

TEMPORAL_IMAGE = "temporalio/temporal@sha256:ad4c82c97bd12b417d1ea942610dbcd511afb250c4d5ed26c694009533df447e"
_DRILL_PREFIX = "aether-drill-"
_CONTAINER_NAME = re.compile(r"aether-drill-[a-z0-9]{8}")
_DOCKER = "docker"
_STATUS = "status"
_TEMPORAL_DB = "temporal_db"
_WORKFLOW_ID = "workflow_id"


def main() -> None:
    parser = argparse.ArgumentParser(description="备份、模拟崩溃、恢复，并与 Temporal 对账")
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    _add_common(backup)
    backup.add_argument("--artifact-root", type=Path, required=True)
    backup.add_argument("--workflow-id", required=True)
    crash = sub.add_parser("crash")
    _add_common(crash)
    restore = sub.add_parser("restore")
    _add_common(restore)
    restore.add_argument("--artifact-root", type=Path, required=True)
    restore.add_argument("--confirm", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--database-url", required=True)
    reconcile.add_argument("--destination", type=Path, required=True)
    reconcile.add_argument("--temporal-address", required=True)
    reconcile.add_argument("--workflow-id", required=True)
    reconcile.add_argument("--project-id", required=True)
    reconcile.add_argument("--task-id", required=True)
    reconcile.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backup":
        _backup(args)
    elif args.command == "crash":
        _crash(args)
    elif args.command == "restore":
        _restore(args)
    elif args.command == "reconcile":
        _reconcile(args)
    else:
        raise SystemExit("未知命令")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--temporal-container", required=True)
    parser.add_argument("--temporal-db", type=Path, required=True)
    parser.add_argument("--temporal-address", required=True)


def _backup(args: argparse.Namespace) -> None:
    if not str(args.temporal_container).startswith(_DRILL_PREFIX):
        raise SystemExit("备份只接受演练容器")
    workflow_id = accepted_workflow_id(args.workflow_id)
    _docker_stop(args.temporal_container)
    copied = copy_temporal_files(args.temporal_db.parent, args.destination / "temporal")
    manifest = backup_project(args.database_url, args.artifact_root, args.destination)
    _docker_start(args.temporal_container)
    described = _wait_temporal(args.temporal_address, workflow_id)
    write_temporal_snapshot(args.destination, described)
    write_marker(args.destination, container=args.temporal_container, temporal_db=args.temporal_db, workflow_id=workflow_id)
    print(json.dumps({"copied": copied, "sha256": manifest["sha256"], "temporal_status": described[_STATUS]}, sort_keys=True))


def _crash(args: argparse.Namespace) -> None:
    marker = assert_crash_target(args.destination, container=args.temporal_container, temporal_db=args.temporal_db)
    _docker_stop(args.temporal_container)
    removed = remove_temporal_files(Path(marker[_TEMPORAL_DB]).parent)
    print(json.dumps({"removed": removed}, sort_keys=True))


def _restore(args: argparse.Namespace) -> None:
    marker = assert_crash_target(args.destination, container=args.temporal_container, temporal_db=args.temporal_db)
    assert_restore_ready(args.destination, args.confirm)
    state_dir = Path(marker[_TEMPORAL_DB]).parent
    _docker_stop(args.temporal_container)
    for name in ("temporal.db", "temporal.db-wal", "temporal.db-shm"):
        live = state_dir / name
        if live.exists():
            live.unlink()
    copy_temporal_files(args.destination / "temporal", state_dir)
    _docker_start(args.temporal_container)
    _wait_temporal(args.temporal_address, _workflow_id(args.destination))
    restored = restore_project(args.database_url, args.artifact_root, args.destination, confirm=args.confirm)
    public = {
        "dispatch": restored.get("dispatch"),
        "kept_live_tasks": restored.get("kept_live_tasks", []),
        "pending_after_restore": restored.get("pending_after_restore"),
        "volume_deleted": restored.get("volume_deleted"),
    }
    print(json.dumps(public, ensure_ascii=False, sort_keys=True, default=str))


def _reconcile(args: argparse.Namespace) -> None:
    workflow_id = accepted_workflow_id(args.workflow_id)
    described = describe_workflow(args.temporal_address, workflow_id)
    status = _task_status(args.database_url, args.project_id, args.task_id)
    report = reconcile_observation(db_status=status, temporal_status=str(described[_STATUS]), run_id=str(described["run_id"]))
    report[_WORKFLOW_ID] = workflow_id
    report["note"] = "恢复是合并。Temporal 历史不随数据库回退。终态没有被改写。"
    write_report(args.report, report)
    print(json.dumps({"relation": report["relation"], "action": report["action"], "rewritten": False}, sort_keys=True))


def describe_workflow(address: str, workflow_id: str) -> dict[str, str]:
    return asyncio.run(_describe(address, workflow_id))


async def _describe(address: str, workflow_id: str) -> dict[str, str]:
    client = await Client.connect(address)
    handle = client.get_workflow_handle(workflow_id)
    described = await handle.describe()
    status = described.status.name if described.status is not None else "UNKNOWN"
    return {_WORKFLOW_ID: workflow_id, "run_id": described.run_id, _STATUS: status}


def _task_status(database_url: str, project_id: str, task_id: str) -> str:
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE project_id = %s AND id = %s", (project_id, task_id)).fetchone()
    if row is None:
        raise SystemExit("任务不在数据库里")
    return str(row[0])


def assert_restore_ready(destination: Path, confirm: str) -> None:
    """Refuse before either live store is replaced."""
    if confirm != "restore-aether":
        raise SystemExit("恢复确认无效")
    root = confine(destination)
    manifest_path = root / "manifest.json"
    tables_path = root / "tables.json"
    temporal_path = root / "temporal" / "temporal.db"
    if not manifest_path.is_file() or not tables_path.is_file() or not temporal_path.is_file():
        raise SystemExit("恢复备份不完整")
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(tables_path.read_bytes()).hexdigest()
    if not isinstance(recorded, dict) or digest != recorded.get("sha256"):
        raise SystemExit("恢复备份哈希不符")


def _safe_container(name: str) -> str:
    matched = _CONTAINER_NAME.fullmatch(name)
    if matched is None:
        raise SystemExit("演练容器名称被拒绝")
    return matched.group(0)


def _docker_stop(name: str) -> None:
    _run_docker("stop", _safe_container(name))


def _docker_start(name: str) -> None:
    _run_docker("start", _safe_container(name))


def _run_docker(action: str, name: str) -> None:
    if action == "stop":
        argv = [_DOCKER, "stop", name]
    elif action == "start":
        argv = [_DOCKER, "start", name]
    else:
        raise SystemExit("docker 动作被拒绝")
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else ""
        raise SystemExit(f"docker {action} 失败: {tail}")


def _wait_temporal(address: str, workflow_id: str) -> dict[str, str]:
    deadline = time.monotonic() + 40
    last = "unreachable"
    while time.monotonic() < deadline:
        try:
            return describe_workflow(address, workflow_id)
        except Exception as exc:
            last = type(exc).__name__
        time.sleep(0.4)
    raise SystemExit(f"演练 Temporal 没有就绪: {last}")


def _workflow_id(destination: Path) -> str:
    marker = json.loads((destination / "crash-drill.json").read_text(encoding="utf-8"))
    return accepted_workflow_id(str(marker.get(_WORKFLOW_ID) or ""))


def start_drill_temporal(*, name: str, port: int, state_dir: Path) -> None:
    """Test helper. The image is the pinned temporal digest."""
    safe = _safe_container(name)
    if port <= 0 or port > 65535:
        raise SystemExit("演练端口被拒绝")
    root = confine(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            _DOCKER,
            "run",
            "-d",
            "--name",
            safe,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "USER=temporal",
            "-e",
            "HOME=/state",
            "-p",
            f"127.0.0.1:{port}:7233",
            "-v",
            f"{root}:/state",
            TEMPORAL_IMAGE,
            "server",
            "start-dev",
            "--ip",
            "0.0.0.0",
            "--port",
            "7233",
            "--headless",
            "--log-format",
            "json",
            "--db-filename",
            "/state/temporal.db",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise SystemExit("演练 Temporal 没有启动: " + (tail[-1] if tail else ""))
    _wait_port(port, safe)


def stop_drill_temporal(name: str) -> None:
    safe = _safe_container(name)
    subprocess.run([_DOCKER, "rm", "-f", safe], capture_output=True, text=True, check=False)


def _wait_port(port: int, name: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not _container_running(name):
            raise SystemExit("演练 Temporal 已退出: " + _logs(name))
        if _accepting(port):
            time.sleep(0.4)
            if _container_running(name) and _accepting(port):
                return
            continue
        time.sleep(0.3)
    raise SystemExit("演练 Temporal 没有监听: " + _logs(name))


def _accepting(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.2)
    try:
        sock.connect(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _container_running(name: str) -> bool:
    completed = subprocess.run(
        [_DOCKER, "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "running"


def _logs(name: str) -> str:
    completed = subprocess.run([_DOCKER, "logs", name], capture_output=True, text=True, check=False)
    text = (completed.stderr or completed.stdout or "").strip().splitlines()
    return text[-1] if text else "没有日志"


if __name__ == "__main__":
    main()
