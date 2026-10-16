"""P0-02 · 一条普通的重复/悬空命令让控制台与记忆命令整体卡死，并让 worker 主循环退出。

不变量 INV-STAB-01：单条命令失败只能把这一条标成 FAILED，不能回滚同批其他命令，
不能让 route_pending / serve() 抛出，更不能让其他项目的任务停摆。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.service import route_pending
from audit_helpers import key, project, second_project, store_for

ROOT = Path(__file__).resolve().parents[4]


def _outbox(database: Database, owner: str, project_id: str) -> dict[str, str]:
    with database.transaction(owner_id=owner, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT command_key, status FROM command_outbox WHERE project_id = :p"),
            {"p": project_id},
        ).mappings().all()
    return {str(row["command_key"]): str(row["status"]) for row in found}


def _poison(client, project_id: str, headers: dict[str, str], variant: str) -> tuple[str, str]:
    """Return (first_command, poison_command). Both are accepted with HTTP 202."""
    if variant == "duplicate_source_locator":
        body = {"kind": "url", "locator": "https://example.test/same-page", "allowed_domains": ["example.test"]}
        first = client.post(f"/v1/projects/{project_id}/sources", headers={**headers, "Idempotency-Key": key()}, json=body)
        second = client.post(f"/v1/projects/{project_id}/sources", headers={**headers, "Idempotency-Key": key()}, json=body)
    elif variant == "feedback_for_unknown_source":
        body = {"kind": "url", "locator": "https://example.test/real", "allowed_domains": ["example.test"]}
        first = client.post(f"/v1/projects/{project_id}/sources", headers={**headers, "Idempotency-Key": key()}, json=body)
        second = client.post(
            f"/v1/projects/{project_id}/feedback",
            headers={**headers, "Idempotency-Key": key()},
            json={"event_id": str(uuid.uuid4()), "source_id": str(uuid.uuid4()), "kind": "click", "event_time": "2026-10-16T00:00:00Z"},
        )
    elif variant == "duplicate_connector_key":
        body = {"project_id": project_id, "connector_key": "docs", "transport": "stdio", "manifest": {"argv": ["python3", "server.py"]}}
        first = client.post("/v1/admin/connectors", headers={**headers, "Idempotency-Key": key()}, json=body)
        second = client.post("/v1/admin/connectors", headers={**headers, "Idempotency-Key": key()}, json=body)
    else:
        raise AssertionError(variant)
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    return first.json()["command_id"], second.json()["command_id"]


@pytest.mark.parametrize("variant", ["duplicate_source_locator", "feedback_for_unknown_source", "duplicate_connector_key"])
def test_one_bad_console_command_does_not_wedge_the_batch(client, role_urls, variant) -> None:
    owner, project_id, headers = project(client)
    first, poison = _poison(client, project_id, headers, variant)
    later = client.post(
        f"/v1/projects/{project_id}/sources",
        headers={**headers, "Idempotency-Key": key()},
        json={"kind": "url", "locator": "https://example.test/unrelated", "allowed_domains": ["example.test"]},
    )
    assert later.status_code == 202, later.text
    other = second_project(client, headers)
    other_cmd = client.post(
        f"/v1/projects/{other}/sources",
        headers={**headers, "Idempotency-Key": key()},
        json={"kind": "url", "locator": "https://example.test/other-project", "allowed_domains": ["example.test"]},
    )
    assert other_cmd.status_code == 202, other_cmd.text

    dispatcher = Database(role_urls["aether_dispatcher"])
    worker = Database(role_urls["aether_worker"])
    verifier = Database(role_urls["aether_verifier"])
    error: BaseException | None = None
    try:
        route_pending(dispatcher, worker, verifier, store_for(client, role_urls))
    except Exception as exc:  # the invariant is that this never escapes
        error = exc
    statuses = _outbox(worker, owner, project_id)
    other_status = _outbox(worker, owner, other)
    detail = (
        f"variant={variant} error={type(error).__name__ if error else None}: {str(error)[:160] if error else ''} "
        f"first={statuses.get(first)} poison={statuses.get(poison)} later={statuses.get(later.json()['command_id'])} "
        f"other_project={other_status.get(other_cmd.json()['command_id'])}"
    )
    assert error is None, detail
    assert statuses[poison] == "FAILED", detail
    assert statuses[first] == "DELIVERED", detail
    assert statuses[later.json()["command_id"]] == "DELIVERED", detail
    assert other_status[other_cmd.json()["command_id"]] == "DELIVERED", detail


def _drain(pipe, bucket: list[str]) -> None:
    for line in pipe:
        bucket.append(line)


def test_documented_worker_survives_a_duplicate_source_and_keeps_running_tasks(client, role_urls, tmp_path) -> None:
    """Process level: python -m aether_worker against real PostgreSQL and Temporal."""
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if not address:
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    owner, project_id, headers = project(client)
    _poison(client, project_id, headers, "duplicate_source_locator")
    other = second_project(client, headers)
    fact = "事实：水在标准大气压下于一百摄氏度沸腾。"
    uploaded = client.post(
        f"/v1/projects/{other}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("note.txt", f"{fact}\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    task = client.post(f"/v1/projects/{other}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "请根据已上传资料回答沸点"})
    assert task.status_code == 202, task.text
    task_id = task.json()["resource_id"]

    env = os.environ.copy()
    env.update(
        {
            "AETHER_WORKER_DATABASE_URL": role_urls["aether_worker"],
            "AETHER_VERIFIER_DATABASE_URL": role_urls["aether_verifier"],
            "AETHER_DISPATCHER_DATABASE_URL": role_urls["aether_dispatcher"],
            "AETHER_TEMPORAL_ADDRESS": address,
            "AETHER_ARTIFACT_ROOT": str(client.app.state.settings.artifact_root),
            "AETHER_TASK_QUEUE": f"audit-poison-{uuid.uuid4()}",
            "AETHER_RESEARCH_PROVIDER": "scripted",
        }
    )
    logs: list[str] = []
    proc = subprocess.Popen([sys.executable, "-m", "aether_worker"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=_drain, args=(proc.stdout, logs), daemon=True).start()
    worker = Database(role_urls["aether_worker"])
    status = None
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            with worker.transaction(owner_id=owner, project_id=other) as conn:
                status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
            if status == "SUCCEEDED" or proc.poll() is not None:
                break
            time.sleep(0.5)
        exited = proc.poll()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    tail = "".join(logs[-12:])
    assert exited is None, f"worker 进程退出 returncode={exited}，另一项目的任务状态={status}\n{tail}"
    assert status == "SUCCEEDED", f"另一项目的任务状态={status}\n{tail}"


def test_memory_claim_matching_deleted_bytes_does_not_wedge_memory_commands(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    claim = "戊服务的维护窗口是每周日凌晨。"
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("note.txt", claim.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    removed = client.delete(f"/v1/projects/{project_id}/artifacts/{uploaded.json()['artifact_id']}", headers={**headers, "Idempotency-Key": key()})
    assert removed.status_code == 204, removed.text
    first = client.post(f"/v1/projects/{project_id}/memories", headers={**headers, "Idempotency-Key": key()}, json={"claim": claim, "kind": "semantic", "category": "fact"})
    later = client.post(
        f"/v1/projects/{project_id}/memories",
        headers={**headers, "Idempotency-Key": key()},
        json={"claim": "己服务的负责人是平台组。", "kind": "semantic", "category": "preference"},
    )
    assert first.status_code == 202 and later.status_code == 202
    error: BaseException | None = None
    try:
        route_pending(Database(role_urls["aether_dispatcher"]), Database(role_urls["aether_worker"]), Database(role_urls["aether_verifier"]), store_for(client, role_urls))
    except Exception as exc:
        error = exc
    statuses = _outbox(Database(role_urls["aether_worker"]), owner, project_id)
    detail = f"error={type(error).__name__ if error else None}: {error} first={statuses.get(first.json()['command_id'])} later={statuses.get(later.json()['command_id'])}"
    assert error is None, detail
    assert statuses[later.json()["command_id"]] == "DELIVERED", detail
