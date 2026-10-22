"""SYS-01 process proof. Commands start at the public API; the production worker reaches a terminal state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from temporalio.client import Client, ScheduleActionStartWorkflow

from aether_worker.startup_budget import wait_worker_ready
from aether_worker.startup_budget import ActiveDeadline
from aether_api.auth_service import issue_bootstrap_secret
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from tests.integration.test_hybrid_retrieval import APPLE
from tests.integration.test_worker_process import (
    ORIGIN_VALUE,
    PASSPHRASE,
    ROOT,
    _headers,
    _port,
    _service_env,
    _start,
    _stop,
    _temporal_address,
    _wait_http,
)


def _poll(http: httpx.Client, url: str, ready: Callable[[dict[str, Any]], Any], proc, logs: list[str], timeout: float = 90):
    deadline = ActiveDeadline(timeout)
    last = ""
    while not deadline.expired(allow=10):
        if proc.poll() is not None:
            raise AssertionError(f"worker 退出 {proc.returncode}: {''.join(logs)}")
        response = http.get(url)
        last = response.text
        if response.status_code == 200:
            found = ready(response.json())
            if found is not None:
                return found
        time.sleep(0.3)
    raise AssertionError(last + "\n" + "".join(logs))


def _command_status(database: Database, owner_id: str, project_id: str, command_id: str) -> str | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return conn.execute(
            text("SELECT status FROM command_outbox WHERE project_id = :project_id AND command_key = :id"),
            {"project_id": project_id, "id": command_id},
        ).scalar()


def _wait_command(database: Database, owner_id: str, project_id: str, command_id: str, expected: str, proc, logs: list[str]) -> None:
    deadline = ActiveDeadline(30)
    last = ""
    while not deadline.expired():
        if proc.poll() is not None:
            raise AssertionError(f"worker 退出 {proc.returncode}: {''.join(logs[-40:])}")
        last = _command_status(database, owner_id, project_id, command_id) or ""
        if last == expected:
            return
        time.sleep(0.2)
    raise AssertionError(f"命令停在 {last}: {''.join(logs[-40:])}")


def test_public_entry_reaches_a_terminal_state(role_urls, clean_db, tmp_path: Path) -> None:
    address = _temporal_address()
    api_port = _port()
    queue = f"aether-sys01-{uuid.uuid4()}"
    artifact_root = tmp_path / "artifacts"
    index_root = tmp_path / "indexes"
    secret_path = ROOT / "secrets" / f"sys01-{uuid.uuid4().hex}.json"
    secret = issue_bootstrap_secret(secret_path)
    env = _service_env(
        role_urls,
        address=address,
        api_port=api_port,
        queue=queue,
        artifact_root=artifact_root,
        secret_path=secret_path,
        index_root=index_root,
    )
    api_logs: list[str] = []
    worker_logs: list[str] = []
    api = _start([sys.executable, "-m", "aether_api.main"], env, api_logs)
    worker = _start([sys.executable, "-m", "aether_worker"], env, worker_logs)
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        wait_worker_ready(worker_logs, worker)
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=10) as http:
            _exercise(http, role_urls, worker, worker_logs, secret, artifact_root, queue)
    finally:
        _stop(worker)
        _stop(api)
        secret_path.unlink(missing_ok=True)


def _exercise(http: httpx.Client, role_urls, worker, worker_logs: list[str], secret: str, artifact_root: Path, task_queue: str) -> None:
    bootstrap = http.post(
        "/v1/auth/bootstrap",
        headers={"Origin": ORIGIN_VALUE},
        json={"bootstrap_secret": secret, "display_name": "所有者", "passphrase": PASSPHRASE},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    csrf = bootstrap.json()["csrf_token"]
    owner_id = bootstrap.json()["owner_id"]
    project = http.post("/v1/projects", headers=_headers(csrf, "sys01-project"), json={"name": "入口"})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    uploaded = http.post(
        f"/v1/projects/{project_id}/artifacts",
        headers=_headers(csrf, "sys01-artifact"),
        files={"file": ("apple.txt", APPLE.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    source = http.post(
        f"/v1/projects/{project_id}/sources",
        headers=_headers(csrf, "sys01-source"),
        json={"kind": "local_document", "locator": uploaded.json()["artifact_id"], "allowed_domains": ["example.test"]},
    )
    assert source.status_code == 202, source.text
    listed = _poll(
        http,
        f"/v1/projects/{project_id}/sources",
        lambda body: body["items"][0] if body["items"] else None,
        worker,
        worker_logs,
    )
    source_id = listed["id"]
    ingested = http.post(
        f"/v1/projects/{project_id}/ingestions",
        headers=_headers(csrf, "sys01-ingest"),
        json={"source_id": source_id},
    )
    assert ingested.status_code == 202, ingested.text
    job = _poll(
        http,
        f"/v1/projects/{project_id}/ingestions",
        lambda body: next((item for item in body["items"] if item["status"] in {"SUCCEEDED", "FAILED"}), None),
        worker,
        worker_logs,
    )
    assert job["status"] == "SUCCEEDED", job
    assert job["error_code"] is None
    assert job["source_version_id"]
    visible = http.get(f"/v1/projects/{project_id}/sources/{source_id}")
    assert visible.status_code == 200, visible.text
    assert visible.json()["index_status"] == "indexed"
    assert visible.json()["latest_source_version_id"] == job["source_version_id"]
    found = http.post(
        f"/v1/projects/{project_id}/search",
        headers=_headers(csrf, "sys01-search"),
        json={"project_id": project_id, "query": "苹果派", "mode": "hybrid"},
    )
    assert found.status_code == 200, found.text + "".join(worker_logs[-40:])
    assert found.json()["hits"][0]["text"] == APPLE
    assert found.json()["hits"][0]["document_id"] == job["source_version_id"]
    _assert_missing_jobs_are_not_queued(http, csrf, project_id, role_urls, owner_id)
    _assert_watch(http, csrf, project_id, source_id, worker, worker_logs, role_urls, owner_id, artifact_root, task_queue)


def _assert_missing_jobs_are_not_queued(http: httpx.Client, csrf: str, project_id: str, role_urls, owner_id: str) -> None:
    coding = http.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "sys01-code"),
        json={"task_id": str(uuid.uuid4()), "base_artifact_id": str(uuid.uuid4()), "job_key": "edit", "objective": "改一处"},
    )
    assert coding.status_code == 404, coding.text
    assert coding.json()["code"] == "not_found"
    skill = http.post(
        f"/v1/projects/{project_id}/skills/{uuid.uuid4()}/evaluate",
        headers=_headers(csrf, "sys01-skill"),
        json={
            "candidate_version_id": str(uuid.uuid4()),
            "dataset_manifest_id": str(uuid.uuid4()),
            "budget": {"max_tokens": 10},
        },
    )
    assert skill.status_code == 404, skill.text
    assert skill.json()["code"] == "not_found"
    evaluations = http.get(f"/v1/projects/{project_id}/evaluations")
    assert evaluations.status_code == 200
    assert evaluations.json()["items"] == []
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        jobs = conn.execute(text("SELECT count(*) FROM coding_jobs WHERE project_id = :project_id"), {"project_id": project_id}).scalar_one()
        runs = conn.execute(text("SELECT count(*) FROM evaluation_runs WHERE project_id = :project_id"), {"project_id": project_id}).scalar_one()
    assert int(jobs) == 0
    assert int(runs) == 0


def _assert_watch(http, csrf, project_id, source_id, worker_proc, worker_logs, role_urls, owner_id, artifact_root: Path, task_queue: str) -> None:
    created = http.post(
        f"/v1/projects/{project_id}/watches",
        headers=_headers(csrf, "sys01-watch"),
        json={
            "purpose": "跟踪沸点",
            "source_ids": [source_id],
            "schedule": {"interval_seconds": 3600, "timezone": "Australia/Melbourne"},
            "budget": {"max_tokens": 100},
        },
    )
    assert created.status_code == 202, created.text
    draft = _poll(
        http,
        f"/v1/projects/{project_id}/watches",
        lambda body: next((item for item in body["items"] if item["status"] == "DRAFT"), None),
        worker_proc,
        worker_logs,
    )
    database = Database(role_urls["aether_worker"])
    blocked = http.patch(
        f"/v1/projects/{project_id}/watches/{draft['id']}",
        headers={**_headers(csrf, "sys01-watch-block"), "If-Match": "1"},
        json={"status": "ACTIVE"},
    )
    assert blocked.status_code == 202, blocked.text
    _wait_command(database, owner_id, project_id, blocked.json()["command_id"], "FAILED", worker_proc, worker_logs)
    still = http.get(f"/v1/projects/{project_id}/watches")
    assert still.status_code == 200
    current = next(item for item in still.json()["items"] if item["id"] == draft["id"])
    assert current["status"] == "DRAFT"
    assert current["last_run_at"] is None
    approved = http.patch(
        f"/v1/projects/{project_id}/watches/{draft['id']}",
        headers={**_headers(csrf, "sys01-watch-run"), "If-Match": str(current["version"])},
        json={"status": "ACTIVE", "expected_source_ids": [source_id]},
    )
    assert approved.status_code == 202, approved.text
    ran = _poll(
        http,
        f"/v1/projects/{project_id}/watches",
        lambda body: next((item for item in body["items"] if item["id"] == draft["id"] and item["last_run_at"]), None),
        worker_proc,
        worker_logs,
    )
    assert ran["status"] == "ACTIVE"
    _assert_watch_records(database, owner_id, project_id, draft["id"], artifact_root)
    _assert_watch_schedule(database, owner_id, project_id, draft["id"], task_queue)


def _assert_watch_records(database: Database, owner_id: str, project_id: str, watch_id: str, artifact_root: Path) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        note = conn.execute(
            text("SELECT content FROM notifications WHERE project_id = :project_id AND watch_id = :watch_id"),
            {"project_id": project_id, "watch_id": watch_id},
        ).scalar_one()
        rows = conn.execute(
            text("SELECT id::text AS id, sha256 FROM artifacts WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).mappings().all()
    content = note if isinstance(note, dict) else json.loads(note)
    assert content["external_sent"] is False
    store = ArtifactStore(artifact_root, database)
    matched = False
    for row in rows:
        data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=row["id"])
        if data.startswith(b"watch-run\n") and watch_id.encode() in data:
            assert hashlib.sha256(data).hexdigest() == row["sha256"]
            matched = True
    assert matched


def _assert_watch_schedule(database: Database, owner_id: str, project_id: str, watch_id: str, task_queue: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        schedule_id = conn.execute(
            text("SELECT schedule_id FROM watches WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": watch_id},
        ).scalar_one()
    client = asyncio.run(Client.connect(_temporal_address()))

    async def describe_ready():
        deadline = ActiveDeadline(30)
        last = None
        while not deadline.expired():
            described = await client.get_schedule_handle(str(schedule_id)).describe()
            last = described
            if described.info.num_actions >= 1 and described.info.next_action_times:
                return described
            await asyncio.sleep(0.3)
        raise AssertionError(f"schedule 没有循环动作: {last}")

    described = asyncio.run(describe_ready())
    action = described.schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.task_queue == task_queue
    assert described.schedule.spec.intervals[0].every == timedelta(seconds=3600)
    assert described.info.num_actions >= 1
    assert list(described.info.next_action_times)
