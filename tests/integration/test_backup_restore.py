import json
from pathlib import Path

import psycopg
from psycopg import sql

from aether_worker.outbox import enqueue_task
from aether_worker.supply import backup_project, restore_project
from tests.integration.test_auth import ORIGIN, _bootstrap


def test_restore_stops_commands_and_writes_snapshot_rows_back(client, api_db, aether_admin: str, tmp_path: Path) -> None:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={**ORIGIN, "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "backup-project"},
        json={"name": "备份"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    enqueue_task(api_db, owner_id=session["owner_id"], project_id=project_id, goal="待对账", command_key="backup-pending")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "note.txt").write_text("原始字节", encoding="utf-8")
    destination = tmp_path / "backup"
    manifest = backup_project(aether_admin, artifacts, destination)
    assert manifest["volume_deleted"] is False
    assert manifest["pending_commands"]
    snapshot = json.loads((destination / "tables.json").read_text(encoding="utf-8"))
    (artifacts / "note.txt").write_text("被改掉", encoding="utf-8")
    with psycopg.connect(aether_admin) as conn:
        conn.execute("UPDATE projects SET name = '被改掉' WHERE id = %s", (project_id,))
        conn.commit()
    restored = restore_project(aether_admin, artifacts, destination, confirm="restore-aether")
    assert (artifacts / "note.txt").read_text(encoding="utf-8") == "原始字节"
    assert restored["volume_deleted"] is False
    assert restored["dispatch"] == "stopped"
    assert restored["pending_after_restore"] == 0
    assert restored["stopped_commands"]
    with psycopg.connect(aether_admin) as conn:
        status = conn.execute("SELECT status FROM command_outbox WHERE command_key = 'backup-pending'").fetchone()
        name = conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE original_goal = '待对账'").fetchone()
        claimable = conn.execute(
            "SELECT count(*) FROM command_outbox WHERE status IN ('PENDING', 'DELIVERING')"
        ).fetchone()
        for table in ("projects", "tasks", "artifacts", "command_outbox"):
            for row in snapshot[table]:
                found = conn.execute(
                    sql.SQL("SELECT id::text FROM {} WHERE id = %s").format(sql.Identifier(table)),
                    (row["id"],),
                ).fetchone()
                assert found is not None
                assert found[0] == row["id"]
    assert status is not None and status[0] == "STOPPED"
    assert name == ("备份",)
    assert task == ("QUEUED",)
    assert claimable == (0,)
