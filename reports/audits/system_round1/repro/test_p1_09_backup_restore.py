"""P1-09 · 备份/恢复：恢复会删掉备份之后写入的产物字节，遇到已完成任务会整体中止，也不能在新库上重建。

不变量 INV-DATA-04：恢复不能毁掉恢复点之后仍被账本引用的字节；
不变量 INV-STAB-05：备份能在一个空库上重建出可用的系统（所有者、合同、验收证据、预算、记忆）。
"""

from __future__ import annotations

import psycopg
from aether_storage.bootstrap import ensure_database
from aether_worker.goal_effect import materialize_goal_effect
from aether_worker.supply import SBOM_TABLES, backup_project, restore_project
from audit_helpers import endpoints, key, project, store_for

FRESH = "aether_audit_restore"
FACT = "事实：水在标准大气压下于一百摄氏度沸腾。"


def _upload(client, project_id: str, headers: dict[str, str], body: str) -> str:
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("note.txt", body.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["artifact_id"]


def test_restore_keeps_bytes_written_after_the_backup(client, role_urls, aether_admin, tmp_path) -> None:
    _owner, project_id, headers = project(client)
    _upload(client, project_id, headers, "备份之前的资料。\n")
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    later = _upload(client, project_id, headers, "备份之后才写入的资料。\n")
    restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    error = None
    try:
        response = client.get(f"/v1/projects/{project_id}/artifacts/{later}")
        status = response.status_code
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = None
    assert error is None and status == 200, f"恢复后，账本里还在的产物 {later} 读取结果 status={status} error={error}"


def test_restore_does_not_abort_when_a_task_finished_after_the_backup(client, role_urls, aether_admin, tmp_path) -> None:
    _owner, project_id, headers = project(client)
    _upload(client, project_id, headers, f"{FACT}\n")
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "请根据已上传资料回答沸点"})
    assert created.status_code == 202, created.text
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    assert materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root, provider="scripted"), payload)["status"] == "SUCCEEDED"
    error = None
    try:
        restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    assert error is None, f"任务在备份后完成，恢复整体中止：{error}"


def test_backup_rebuilds_a_fresh_database(client, role_urls, admin_url, aether_admin, tmp_path) -> None:
    _owner, project_id, headers = project(client)
    _upload(client, project_id, headers, f"{FACT}\n")
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "请根据已上传资料回答沸点"})
    assert created.status_code == 202, created.text
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    assert materialize_goal_effect(endpoints(role_urls, store_for(client, role_urls).root, provider="scripted"), payload)["status"] == "SUCCEEDED"
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (FRESH,))
        conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(FRESH)))
    ensure_database(admin_url, database=FRESH, rotate_passwords=False)
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = FRESH
    fresh = psycopg.conninfo.make_conninfo(**info)
    error = None
    try:
        restore_project(fresh, tmp_path / "fresh-artifacts", tmp_path / "backup", confirm="restore-aether")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (FRESH,))
            conn.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(psycopg.sql.Identifier(FRESH)))
    needed = {"owners", "goal_contracts", "verification_results", "budget_accounts", "model_requests", "task_artifacts", "memories", "task_events"}
    detail = f"备份只含 {list(SBOM_TABLES)}，缺 {sorted(needed - set(SBOM_TABLES))}；在新库恢复：{error}"
    assert error is None, detail
    assert needed <= set(SBOM_TABLES), detail
