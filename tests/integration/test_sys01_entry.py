"""SYS-01 process proof. Commands start at the public API; the production worker reaches a terminal state."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

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
    _start,
    _stop,
    _temporal_address,
    _wait_http,
    _wait_line,
)


def _poll(http: httpx.Client, url: str, ready: Callable[[dict[str, Any]], Any], proc, logs: list[str], timeout: float = 90):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"worker 退出 {proc.returncode}: {''.join(logs[-40:])}")
        response = http.get(url)
        last = response.text
        if response.status_code == 200:
            found = ready(response.json())
            if found is not None:
                return found
        time.sleep(0.3)
    raise AssertionError(last + "".join(logs[-40:]))


def _command_status(database: Database, owner_id: str, project_id: str, command_id: str) -> str | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return conn.execute(
            text("SELECT status FROM command_outbox WHERE project_id = :project_id AND command_key = :id"),
            {"project_id": project_id, "id": command_id},
        ).scalar()


def _wait_command(database: Database, owner_id: str, project_id: str, command_id: str, expected: str, proc, logs: list[str]) -> None:
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
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
    env = os.environ.copy()
    env.update(
        {
            "AETHER_DATABASE_URL": role_urls["aether_api"],
            "AETHER_WORKER_DATABASE_URL": role_urls["aether_worker"],
            "AETHER_VERIFIER_DATABASE_URL": role_urls["aether_verifier"],
            "AETHER_DISPATCHER_DATABASE_URL": role_urls["aether_dispatcher"],
            "AETHER_TEMPORAL_ADDRESS": address,
            "AETHER_ARTIFACT_ROOT": str(artifact_root),
            "AETHER_INDEX_ROOT": str(index_root),
            "AETHER_TASK_QUEUE": queue,
            "AETHER_INTAKE_PAUSE_SECONDS": "0",
            "AETHER_RESEARCH_PROVIDER": "scripted",
            "AETHER_BIND_HOST": "127.0.0.1",
            "AETHER_API_PORT": str(api_port),
            "AETHER_BOOTSTRAP_PATH": str(secret_path),
            "AETHER_ORIGINS": ORIGIN_VALUE,
            "AETHER_COOKIE_SECURE": "",
        }
    )
    api_logs: list[str] = []
    worker_logs: list[str] = []
    api = _start([sys.executable, "-m", "aether_api.main"], env, api_logs)
    worker = _start([sys.executable, "-m", "aether_worker"], env, worker_logs)
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        _wait_line(worker_logs, "aether-worker-ready", worker)
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=10) as http:
            _exercise(http, role_urls, worker, worker_logs, secret, artifact_root)
    finally:
        _stop(worker)
        _stop(api)
        secret_path.unlink(missing_ok=True)


def _exercise(http: httpx.Client, role_urls, worker, worker_logs: list[str], secret: str, artifact_root: Path) -> None:
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
    source = http.post(
        f"/v1/projects/{project_id}/sources",
        headers=_headers(csrf, "sys01-source"),
        json={"kind": "local_document", "locator": APPLE, "allowed_domains": ["example.test"]},
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
    _assert_unsupported(http, csrf, project_id, role_urls, owner_id)
    _assert_watch(http, csrf, project_id, source_id, worker, worker_logs, role_urls, owner_id, artifact_root)


def _assert_unsupported(http: httpx.Client, csrf: str, project_id: str, role_urls, owner_id: str) -> None:
    coding = http.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "sys01-code"),
        json={"task_id": str(uuid.uuid4()), "base_artifact_id": str(uuid.uuid4()), "job_key": "edit", "objective": "改一处"},
    )
    assert coding.status_code == 422, coding.text
    assert coding.json()["code"] == "unsupported"
    skill = http.post(
        f"/v1/projects/{project_id}/skills/{uuid.uuid4()}/evaluate",
        headers=_headers(csrf, "sys01-skill"),
        json={
            "candidate_version_id": str(uuid.uuid4()),
            "dataset_manifest_id": str(uuid.uuid4()),
            "budget": {"max_tokens": 10},
        },
    )
    assert skill.status_code == 422, skill.text
    assert skill.json()["code"] == "unsupported"
    evaluations = http.get(f"/v1/projects/{project_id}/evaluations")
    assert evaluations.status_code == 200
    assert evaluations.json()["items"] == []
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        jobs = conn.execute(text("SELECT count(*) FROM coding_jobs WHERE project_id = :project_id"), {"project_id": project_id}).scalar_one()
        runs = conn.execute(text("SELECT count(*) FROM evaluation_runs WHERE project_id = :project_id"), {"project_id": project_id}).scalar_one()
    assert int(jobs) == 0
    assert int(runs) == 0


def _assert_watch(http, csrf, project_id, source_id, worker_proc, worker_logs, role_urls, owner_id, artifact_root: Path) -> None:
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
