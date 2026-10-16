"""System audit round 1, P1-09: what a backup covers and what a restore may overwrite.

INV-DATA-04: a restore does not destroy bytes written after the restore point that the ledger still references.
INV-STAB-05: a backup rebuilds a working system on an empty database (owner, contracts, verifier evidence,
budgets, memories).
"""

from __future__ import annotations

import json
import uuid

import psycopg
from psycopg import sql

from aether_storage.bootstrap import ensure_database
from aether_worker.goal_effect import WorkerEndpoints, materialize_goal_effect
from aether_worker.supply import SBOM_TABLES, backup_project, restore_project
from tests.integration.test_auth import ORIGIN, _bootstrap

FRESH = "aether_round1_restore"
FACT = "事实：水在标准大气压下于一百摄氏度沸腾。"
NEEDED = {"owners", "goal_contracts", "verification_results", "budget_accounts", "model_requests", "task_artifacts", "memories", "task_events"}


def _key() -> str:
    return f"round1-{uuid.uuid4().hex}"


def _project(client) -> tuple[str, dict[str, str]]:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    created = client.post("/v1/projects", headers={**headers, "Idempotency-Key": _key()}, json={"name": "备份恢复"})
    assert created.status_code == 201, created.text
    return created.json()["id"], headers


def _upload(client, project_id: str, headers: dict[str, str], body: str) -> str:
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": _key()},
        files={"file": ("note.txt", body.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["artifact_id"]


def _answered(client, project_id: str, headers: dict[str, str]) -> dict[str, str]:
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": _key()}, json={"goal": "请根据已上传资料回答沸点"})
    assert created.status_code == 202, created.text
    return {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}


def _run(client, role_urls, payload: dict[str, str]) -> dict:
    root = client.app.state.settings.artifact_root
    endpoints = WorkerEndpoints(
        worker_url=role_urls["aether_worker"],
        verifier_url=role_urls["aether_verifier"],
        dispatcher_url=role_urls["aether_dispatcher"],
        artifact_root=root,
        temporal_address="127.0.0.1:9",
        task_queue=f"round1-{uuid.uuid4().hex}",
        research_provider="scripted",
        live_model_base="http://never-contacted.invalid/v1",
        live_model_alias="round1-model",
        live_model_key="synthetic-round1-key",
    )
    return materialize_goal_effect(endpoints, payload)


def _seed_ledgers(url: str, project_id: str, task_id: str, artifact_id: str) -> None:
    """The scripted run opens no budget, makes no model call and writes no memory; add those, plus a superseding memory."""
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO budget_accounts (project_id, task_id, limit_micro_usd, held_micro_usd, token_limit, max_requests) "
            "VALUES (%s, %s, 1000, 10, 1000, 3)",
            (project_id, task_id),
        )
        conn.execute(
            "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status) "
            "VALUES (%s, %s, 'round1-call', %s, 'answer', 'round1-model', 10, 'reserved')",
            (project_id, task_id, "c" * 64),
        )
        for memory_id, revision, supersedes in ((first, 1, None), (second, 2, first)):
            conn.execute(
                "INSERT INTO memories (project_id, id, memory_key, revision, kind, claim, category, claim_artifact_id, provenance, epistemic_status, supersedes_id) "
                "VALUES (%s, %s, 'boiling-point', %s, 'fact', %s, 'science', %s, '{}'::jsonb, 'user_confirmed', %s)",
                (project_id, memory_id, revision, FACT, artifact_id, supersedes),
            )


def _counts(url: str) -> dict[str, int]:
    with psycopg.connect(url) as conn:
        return {table: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0] for table in SBOM_TABLES}


def _drop(admin_url: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (FRESH,))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(FRESH)))


def test_restore_keeps_bytes_written_after_the_backup(client, role_urls, aether_admin, tmp_path) -> None:
    project_id, headers = _project(client)
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
    project_id, headers = _project(client)
    _upload(client, project_id, headers, f"{FACT}\n")
    payload = _answered(client, project_id, headers)
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    assert _run(client, role_urls, payload)["status"] == "SUCCEEDED"
    error = None
    restored = {}
    try:
        restored = restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    assert error is None, f"任务在备份后完成，恢复整体中止：{error}"
    with psycopg.connect(aether_admin) as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id = %s", (payload["task_id"],)).fetchone()
    assert status == ("SUCCEEDED",)
    assert payload["task_id"] in restored["kept_live_tasks"]


def test_backup_rebuilds_a_fresh_database(client, role_urls, admin_url, aether_admin, tmp_path) -> None:
    project_id, headers = _project(client)
    fact = _upload(client, project_id, headers, f"{FACT}\n")
    payload = _answered(client, project_id, headers)
    assert _run(client, role_urls, payload)["status"] == "SUCCEEDED"
    _seed_ledgers(aether_admin, project_id, payload["task_id"], fact)
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    expected = {table: len(rows) for table, rows in json.loads((tmp_path / "backup" / "tables.json").read_text(encoding="utf-8")).items()}
    _drop(admin_url)
    ensure_database(admin_url, database=FRESH, rotate_passwords=False)
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = FRESH
    fresh = psycopg.conninfo.make_conninfo(**info)
    error = None
    rebuilt: dict[str, int] = {}
    try:
        restore_project(fresh, tmp_path / "fresh-artifacts", tmp_path / "backup", confirm="restore-aether")
        rebuilt = _counts(fresh)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        _drop(admin_url)
    detail = f"备份只含 {list(SBOM_TABLES)}，缺 {sorted(NEEDED - set(SBOM_TABLES))}；在新库恢复：{error}"
    assert error is None, detail
    assert NEEDED <= set(SBOM_TABLES), detail
    assert all(expected[table] > 0 for table in NEEDED), {table: expected[table] for table in NEEDED}
    assert rebuilt == expected
    assert (tmp_path / "fresh-artifacts").is_dir()


def test_restore_does_not_bring_back_a_memory_deleted_after_the_backup(client, role_urls, aether_admin, tmp_path) -> None:
    project_id, headers = _project(client)
    fact = _upload(client, project_id, headers, f"{FACT}\n")
    payload = _answered(client, project_id, headers)
    _seed_ledgers(aether_admin, project_id, payload["task_id"], fact)
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    with psycopg.connect(aether_admin) as conn:
        conn.execute("UPDATE memories SET deleted_at = now() WHERE project_id = %s", (project_id,))
        conn.execute("UPDATE projects SET name = '备份之后改的名字' WHERE id = %s", (project_id,))
    restored = restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    with psycopg.connect(aether_admin) as conn:
        live = conn.execute("SELECT count(*) FROM memories WHERE project_id = %s AND deleted_at IS NULL", (project_id,)).fetchone()
        name = conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()
    assert live == (0,)
    assert restored["kept_live_rows"]["memories"] == 2
    assert name == ("备份恢复",)


def _memory(conn, project_id: str, artifact_id: str, revision: int, supersedes: str | None) -> str:
    memory_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO memories (project_id, id, memory_key, revision, kind, claim, category, claim_artifact_id, provenance, epistemic_status, supersedes_id) "
        "VALUES (%s, %s, 'boiling-point', %s, 'fact', %s, 'science', %s, '{}'::jsonb, 'user_confirmed', %s)",
        (project_id, memory_id, revision, FACT, artifact_id, supersedes),
    )
    return memory_id


def test_restore_does_not_revive_a_memory_corrected_after_the_backup(client, role_urls, aether_admin, tmp_path) -> None:
    project_id, headers = _project(client)
    fact = _upload(client, project_id, headers, f"{FACT}\n")
    with psycopg.connect(aether_admin) as conn:
        original = _memory(conn, project_id, fact, 1, None)
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    with psycopg.connect(aether_admin) as conn:
        conn.execute("UPDATE memories SET epistemic_status = 'superseded' WHERE id = %s", (original,))
        corrected = _memory(conn, project_id, fact, 2, original)
    restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    with psycopg.connect(aether_admin) as conn:
        usable = conn.execute(
            "SELECT id::text FROM memories WHERE project_id = %s AND deleted_at IS NULL AND epistemic_status <> 'superseded'",
            (project_id,),
        ).fetchall()
    assert usable == [(corrected,)], f"恢复后仍可用的记忆 {usable}；被更正的 {original} 不应复活"


def test_restore_keeps_the_index_generation_activated_after_the_backup(client, role_urls, aether_admin, tmp_path) -> None:
    project_id, headers = _project(client)
    manifest = _upload(client, project_id, headers, "索引清单\n")
    insert = (
        "INSERT INTO index_generations (project_id, generation_key, tokenizer_version, source_watermark, manifest_artifact_id, state, manifest_sha256, activated_at) "
        "VALUES (%s, %s, 'round1', 1, %s, 'ACTIVE', %s, now()) RETURNING id::text"
    )
    with psycopg.connect(aether_admin) as conn:
        old = conn.execute(insert, (project_id, "before-backup", manifest, "1" * 64)).fetchone()[0]
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    with psycopg.connect(aether_admin) as conn:
        conn.execute("UPDATE index_generations SET state = 'RETIRED' WHERE id = %s", (old,))
        new = conn.execute(insert, (project_id, "after-backup", manifest, "2" * 64)).fetchone()[0]
    error = None
    try:
        restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    assert error is None, f"备份后换了活动索引代，恢复中止：{error}"
    with psycopg.connect(aether_admin) as conn:
        states = dict(conn.execute("SELECT id::text, state FROM index_generations WHERE project_id = %s", (project_id,)).fetchall())
    assert states == {old: "RETIRED", new: "ACTIVE"}


def test_restore_keeps_a_classification_decided_after_the_backup(client, role_urls, aether_admin, tmp_path) -> None:
    project_id, headers = _project(client)
    claim = _upload(client, project_id, headers, f"{FACT}\n")
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    with psycopg.connect(aether_admin) as conn:
        conn.execute("UPDATE artifacts SET classification = 'memory_claim' WHERE id = %s", (claim,))
    restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    with psycopg.connect(aether_admin) as conn:
        classification = conn.execute("SELECT classification FROM artifacts WHERE id = %s", (claim,)).fetchone()
    assert classification == ("memory_claim",), "备份后改成记忆 claim 的产物，恢复后又成了可当资料的 project_private"


def test_a_restore_that_cannot_stage_every_file_changes_nothing(client, role_urls, aether_admin, tmp_path) -> None:
    project_id, headers = _project(client)
    _upload(client, project_id, headers, "备份之前的资料。\n")
    root = client.app.state.settings.artifact_root
    backup_project(aether_admin, root, tmp_path / "backup")
    stored = next(path for path in root.rglob("*") if path.is_file() and path.relative_to(root).parts[0] == project_id)
    stored.write_bytes(b"live bytes that differ from the snapshot\n")
    blocked = tmp_path / "backup" / "artifacts" / "zzzz-blocked" / "file"
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b"cannot be placed\n")
    (root / "zzzz-blocked").write_bytes(b"a live file where the snapshot has a directory\n")
    with psycopg.connect(aether_admin) as conn:
        conn.execute("UPDATE projects SET name = '备份之后改的名字' WHERE id = %s", (project_id,))
    error = None
    try:
        restore_project(aether_admin, root, tmp_path / "backup", confirm="restore-aether")
    except Exception as exc:
        error = type(exc).__name__
    with psycopg.connect(aether_admin) as conn:
        name = conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()
    assert error is not None
    assert stored.read_bytes() == b"live bytes that differ from the snapshot\n", "恢复失败前已经替换了部分产物文件"
    assert name == ("备份之后改的名字",), "产物没能写回，数据库却已按备份恢复"
