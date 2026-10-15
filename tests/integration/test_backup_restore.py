from pathlib import Path

from sqlalchemy import text

from aether_worker.outbox import enqueue_task
from aether_worker.supply import backup_project, restore_project
from tests.integration.test_auth import ORIGIN, _bootstrap


def test_restore_stops_dispatch_and_puts_artifact_bytes_back(client, api_db, aether_admin: str, tmp_path: Path) -> None:
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
    (artifacts / "note.txt").write_text("被改掉", encoding="utf-8")
    restored = restore_project(aether_admin, artifacts, destination, confirm="restore-aether")
    assert (artifacts / "note.txt").read_text(encoding="utf-8") == "原始字节"
    assert restored["volume_deleted"] is False
    assert restored["pending_after_restore"] == 0
    assert restored["dispatch"] == "stopped"
    with api_db.transaction(owner_id=session["owner_id"], project_id=project_id) as conn:
        status = conn.execute(text("SELECT status FROM command_outbox WHERE command_key = 'backup-pending'")).scalar_one()
        task = conn.execute(text("SELECT status FROM tasks WHERE original_goal = '待对账'")).scalar_one()
    assert status == "FAILED"
    assert task == "QUEUED"
