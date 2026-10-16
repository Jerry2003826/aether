"""Crash-recovery drill. Restore merges; it does not rewind Temporal or finished tasks."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

MARKER_NAME = "crash-drill.json"
_TEMPORAL_DB_NAME = "temporal.db"
TEMPORAL_FILES = (_TEMPORAL_DB_NAME, "temporal.db-wal", "temporal.db-shm")
_ENCODING = "utf-8"
_CONTAINER = "container"
_TEMPORAL_DB = "temporal_db"
_IN_PROGRESS = {"DRAFT", "QUEUED", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED", "NEEDS_REVIEW"}
_CONTAINER_NAME = re.compile(r"aether-drill-[a-z0-9]{8}")
_DRILL_DIR = _CONTAINER_NAME
_ALIGNED_TERMINAL = {
    ("SUCCEEDED", "COMPLETED"),
    ("FAILED", "FAILED"),
    ("CANCELLED", "CANCELED"),
    ("CANCELLED", "TERMINATED"),
}
_WORKFLOW_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
_STATUS = re.compile(r"[A-Z][A-Z_]{0,40}")
_RELATION = re.compile(r"aligned|diverged")
_ACTION = re.compile(r"none|human_review")
_NOTE = "恢复是合并。Temporal 历史不随数据库回退。终态没有被改写。"


def assert_crash_target(destination: Path, *, container: str, temporal_db: Path) -> dict[str, str]:
    """Refuse to delete anything that this drill did not record."""
    marker_path = destination / MARKER_NAME
    if not marker_path.is_file():
        raise SystemExit(f"缺少 {MARKER_NAME}")
    marker = json.loads(marker_path.read_text(encoding=_ENCODING))
    if not isinstance(marker, dict):
        raise SystemExit("演练标记无法读取")
    recorded_container = str(marker.get(_CONTAINER) or "")
    recorded_db = str(marker.get(_TEMPORAL_DB) or "")
    if recorded_container != container or not container.startswith("aether-drill-"):
        raise SystemExit("演练标记与容器不符")
    database = drill_database(temporal_db)
    if Path(recorded_db).resolve() != database:
        raise SystemExit("演练标记与数据库文件不符")
    return {_CONTAINER: recorded_container, _TEMPORAL_DB: str(database)}


def drill_database(path: Path) -> Path:
    """Return the resolved sqlite file only when its parent is one drill directory."""
    resolved = confine(path)
    if resolved.name != _TEMPORAL_DB_NAME or _DRILL_DIR.fullmatch(resolved.parent.name) is None:
        raise SystemExit("只允许删除演练目录里的 Temporal 状态")
    return resolved


def confine(path: Path) -> Path:
    """Accept only a resolved path inside the current working directory."""
    resolved = path.resolve()
    if _within(resolved, Path.cwd()):
        return resolved
    raise SystemExit("演练路径必须在当前目录下")


def accepted_workflow_id(value: str) -> str:
    """Return the workflow id only when it is a single token."""
    return _exact(value, _WORKFLOW_ID, "工作流编号被拒绝")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def write_marker(destination: Path, *, container: str, temporal_db: Path, workflow_id: str) -> None:
    root = confine(destination)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        _CONTAINER: _exact(container, _CONTAINER_NAME, "演练容器名称被拒绝"),
        _TEMPORAL_DB: str(confine(temporal_db)),
        "workflow_id": accepted_workflow_id(workflow_id),
    }
    confine(root / MARKER_NAME).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding=_ENCODING)


def copy_temporal_files(source_dir: Path, destination_dir: Path) -> list[str]:
    source_root = confine(source_dir)
    destination_root = confine(destination_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in TEMPORAL_FILES:
        source = source_root / name
        if source.is_file():
            shutil.copy2(confine(source), confine(destination_root / name))
            copied.append(name)
    if _TEMPORAL_DB_NAME not in copied:
        raise SystemExit("没有复制定时数据库文件")
    return copied


def remove_temporal_files(state_dir: Path) -> list[str]:
    root = confine(state_dir)
    if _DRILL_DIR.fullmatch(root.name) is None:
        raise SystemExit("只允许删除演练目录里的 Temporal 状态")
    removed: list[str] = []
    for name in TEMPORAL_FILES:
        path = root / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    return removed


def reconcile_observation(*, db_status: str, temporal_status: str, run_id: str) -> dict[str, object]:
    """Compare one task with one workflow. This does not write either side."""
    in_progress = db_status in _IN_PROGRESS and temporal_status == "RUNNING"
    aligned = in_progress or (db_status, temporal_status) in _ALIGNED_TERMINAL
    relation = "aligned" if aligned else "diverged"
    action = "none" if aligned else "human_review"
    return {
        "db_status": db_status,
        "temporal_status": temporal_status,
        "run_id": run_id,
        "relation": relation,
        "action": action,
        "rewritten": False,
    }


def write_temporal_snapshot(destination: Path, described: dict[str, str]) -> None:
    root = confine(destination)
    body = {
        "run_id": accepted_workflow_id(str(described.get("run_id") or "")),
        "status": _exact(described.get("status"), _STATUS, "工作流状态被拒绝"),
        "workflow_id": accepted_workflow_id(str(described.get("workflow_id") or "")),
    }
    confine(root / "temporal.json").write_text(json.dumps(body, ensure_ascii=False, sort_keys=True), encoding=_ENCODING)


def write_report(path: Path, report: dict[str, object]) -> None:
    target = confine(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "action": _exact(report.get("action"), _ACTION, "动作被拒绝"),
        "db_status": _exact(report.get("db_status"), _STATUS, "任务状态被拒绝"),
        "note": _NOTE,
        "relation": _exact(report.get("relation"), _RELATION, "关系被拒绝"),
        "rewritten": False,
        "run_id": accepted_workflow_id(str(report.get("run_id") or "")),
        "temporal_status": _exact(report.get("temporal_status"), _STATUS, "工作流状态被拒绝"),
        "workflow_id": accepted_workflow_id(str(report.get("workflow_id") or "")),
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if "postgresql://" in encoded or "AETHER_" in encoded:
        raise SystemExit("演练报告含有连接信息")
    target.write_text(encoded, encoding=_ENCODING)


def _exact(value: object, pattern: re.Pattern[str], message: str) -> str:
    matched = pattern.fullmatch(str(value))
    if matched is None:
        raise SystemExit(message)
    return matched.group(0)
