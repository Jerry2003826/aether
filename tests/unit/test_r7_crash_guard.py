"""Crash drill deletes only the temporal files named by its own marker."""

from __future__ import annotations

import json
import shutil
import uuid
from argparse import Namespace
from pathlib import Path

import pytest

from aether_worker.crash_drill import assert_crash_target, reconcile_observation, remove_temporal_files, write_marker, write_report
from scripts.run_crash_recovery_drill import _restore


def _marker(destination: Path, *, container: str, temporal_db: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "crash-drill.json").write_text(
        json.dumps({"container": container, "temporal_db": str(temporal_db)}),
        encoding="utf-8",
    )


def test_crash_refuses_without_a_marker(tmp_path: Path) -> None:
    state = tmp_path / "aether-drill-state"
    state.mkdir()
    with pytest.raises(SystemExit) as raised:
        assert_crash_target(tmp_path / "backup", container="aether-drill-one", temporal_db=state / "temporal.db")
    assert "crash-drill.json" in str(raised.value)


def test_crash_refuses_a_different_container(tmp_path: Path) -> None:
    state = tmp_path / "aether-drill-state"
    state.mkdir()
    backup = tmp_path / "backup"
    _marker(backup, container="aether-drill-one", temporal_db=state / "temporal.db")
    with pytest.raises(SystemExit):
        assert_crash_target(backup, container="aether-temporal", temporal_db=state / "temporal.db")


def test_remove_temporal_files_only_deletes_the_sqlite_names() -> None:
    root = _workdir()
    try:
        state = root / "aether-drill-aaaaaaaa"
        state.mkdir()
        (state / "temporal.db").write_bytes(b"db")
        (state / "temporal.db-wal").write_bytes(b"wal")
        (state / "keep.txt").write_text("keep", encoding="utf-8")
        removed = remove_temporal_files(state)
        assert set(removed) == {"temporal.db", "temporal.db-wal"}
        assert (state / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert not (state / "temporal.db").exists()
        with pytest.raises(SystemExit, match="只允许删除演练目录"):
            remove_temporal_files(root / "other")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_reconcile_marks_a_finished_task_against_a_running_workflow() -> None:
    report = reconcile_observation(db_status="SUCCEEDED", temporal_status="RUNNING", run_id="run-1")
    assert report["relation"] == "diverged"
    assert report["action"] == "human_review"
    assert report["rewritten"] is False
    again = reconcile_observation(db_status="SUCCEEDED", temporal_status="RUNNING", run_id="run-1")
    assert again == report


def test_write_marker_accepts_workdir_and_refuses_other_roots(tmp_path: Path) -> None:
    root = _workdir()
    try:
        database = root / "aether-drill-state" / "temporal.db"
        write_marker(root / "backup", container="aether-drill-aaaaaaaa", temporal_db=database, workflow_id="task-1")
        marker = json.loads((root / "backup" / "crash-drill.json").read_text(encoding="utf-8"))
        assert marker["workflow_id"] == "task-1"
        assert Path(marker["temporal_db"]) == database.resolve()
        with pytest.raises(SystemExit, match="演练路径必须在"):
            write_marker(Path("/etc"), container="aether-drill-aaaaaaaa", temporal_db=database, workflow_id="task-1")
        with pytest.raises(SystemExit, match="演练路径必须在"):
            write_marker(tmp_path / "backup", container="aether-drill-aaaaaaaa", temporal_db=database, workflow_id="task-1")
        with pytest.raises(SystemExit, match="工作流编号被拒绝"):
            write_marker(root / "other", container="aether-drill-aaaaaaaa", temporal_db=database, workflow_id="../task")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_write_report_keeps_a_checked_workflow_id() -> None:
    root = _workdir()
    try:
        report_path = root / "report.json"
        write_report(
            report_path,
            {
                "action": "human_review",
                "db_status": "SUCCEEDED",
                "relation": "diverged",
                "rewritten": False,
                "run_id": "run-1",
                "temporal_status": "RUNNING",
                "workflow_id": "task-1",
                "note": "ignored",
            },
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["workflow_id"] == "task-1"
        assert report["rewritten"] is False
        assert report["note"] == "恢复是合并。Temporal 历史不随数据库回退。终态没有被改写。"
        with pytest.raises(SystemExit, match="工作流编号被拒绝"):
            write_report(
                report_path,
                {
                    "action": "human_review",
                    "db_status": "SUCCEEDED",
                    "relation": "diverged",
                    "rewritten": False,
                    "run_id": "run-1",
                    "temporal_status": "RUNNING",
                    "workflow_id": "task/1",
                },
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _workdir() -> Path:
    parent = Path.cwd() / ".aether-drill-work"
    parent.mkdir(mode=0o700, exist_ok=True)
    root = parent / uuid.uuid4().hex
    root.mkdir(mode=0o700)
    return root


def test_reconcile_treats_conflicting_terminal_states_as_divergence() -> None:
    for temporal_status in ("FAILED", "CANCELED", "TERMINATED"):
        report = reconcile_observation(db_status="SUCCEEDED", temporal_status=temporal_status, run_id="run-1")
        assert report["relation"] == "diverged"
        assert report["action"] == "human_review"
        assert report["rewritten"] is False
    matched = reconcile_observation(db_status="SUCCEEDED", temporal_status="COMPLETED", run_id="run-1")
    assert matched["relation"] == "aligned"
    failed = reconcile_observation(db_status="FAILED", temporal_status="FAILED", run_id="run-1")
    assert failed["relation"] == "aligned"
    cancelled = reconcile_observation(db_status="CANCELLED", temporal_status="CANCELED", run_id="run-1")
    assert cancelled["relation"] == "aligned"


def test_crash_refuses_a_resolved_directory_that_is_not_the_drill() -> None:
    root = _workdir()
    try:
        live = root / "live-temporal"
        live.mkdir()
        database = live / "temporal.db"
        database.write_bytes(b"live")
        (live / "keep.txt").write_text("keep", encoding="utf-8")
        link = root / "aether-drill-bbbbbbbb"
        link.symlink_to(live, target_is_directory=True)
        destination = root / "backup"
        write_marker(destination, container="aether-drill-aaaaaaaa", temporal_db=database, workflow_id="task-1")
        with pytest.raises(SystemExit, match="只允许删除演练目录"):
            assert_crash_target(destination, container="aether-drill-aaaaaaaa", temporal_db=link / "temporal.db")
        assert (live / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert database.read_bytes() == b"live"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_restore_rejects_confirmation_before_touching_temporal(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workdir()
    try:
        state = root / "aether-drill-aaaaaaaa"
        state.mkdir()
        database = state / "temporal.db"
        database.write_bytes(b"db")
        (state / "keep.txt").write_text("keep", encoding="utf-8")
        destination = root / "backup"
        write_marker(destination, container="aether-drill-aaaaaaaa", temporal_db=database, workflow_id="task-1")
        called: list[str] = []
        monkeypatch.setattr("scripts.run_crash_recovery_drill._docker_stop", lambda name: called.append(name))
        args = _restore_args(destination, database, confirm="nope")
        with pytest.raises(SystemExit, match="恢复确认无效"):
            _restore(args)
        assert called == []
        assert (state / "keep.txt").read_text(encoding="utf-8") == "keep"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _restore_args(destination: Path, database: Path, *, confirm: str) -> Namespace:
    return Namespace(
        destination=destination,
        temporal_container="aether-drill-aaaaaaaa",
        temporal_db=database,
        temporal_address="127.0.0.1:1",
        artifact_root=destination,
        database_url="postgresql://postgres@127.0.0.1/unused",
        confirm=confirm,
    )


def test_restore_rejects_a_bad_hash_before_touching_temporal(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workdir()
    try:
        state = root / "aether-drill-aaaaaaaa"
        state.mkdir()
        database = state / "temporal.db"
        database.write_bytes(b"db")
        destination = root / "backup"
        write_marker(destination, container="aether-drill-aaaaaaaa", temporal_db=database, workflow_id="task-1")
        (destination / "temporal").mkdir()
        (destination / "temporal" / "temporal.db").write_bytes(b"snapshot")
        (destination / "tables.json").write_text("{}", encoding="utf-8")
        (destination / "manifest.json").write_text(
            json.dumps({"sha256": "0" * 64}),
            encoding="utf-8",
        )
        called: list[str] = []
        monkeypatch.setattr("scripts.run_crash_recovery_drill._docker_stop", lambda name: called.append(name))
        args = _restore_args(destination, database, confirm="restore-aether")
        with pytest.raises(SystemExit, match="恢复备份哈希不符"):
            _restore(args)
        assert called == []
        assert database.read_bytes() == b"db"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_reconcile_leaves_an_in_progress_pair_aligned() -> None:
    report = reconcile_observation(db_status="QUEUED", temporal_status="RUNNING", run_id="run-1")
    assert report["relation"] == "aligned"
    assert report["action"] == "none"
    assert report["rewritten"] is False
