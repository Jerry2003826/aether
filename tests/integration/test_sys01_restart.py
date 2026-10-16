"""A restarted worker must finish an ingestion whose command is already DELIVERED."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx

from aether_api.auth_service import issue_bootstrap_secret
from aether_storage.db import Database
from tests.integration.test_hybrid_retrieval import APPLE
from tests.integration.test_sys01_entry import _command_status, _poll
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
    _wait_line,
)


def test_restart_resumes_queued_ingestion_after_delivered_command(role_urls, clean_db, tmp_path: Path) -> None:
    address = _temporal_address()
    api_port = _port()
    queue = f"aether-sys01-restart-{uuid.uuid4()}"
    artifact_root = tmp_path / "artifacts"
    index_root = tmp_path / "indexes"
    secret_path = ROOT / "secrets" / f"sys01-restart-{uuid.uuid4().hex}.json"
    secret = issue_bootstrap_secret(secret_path)
    env = _service_env(
        role_urls,
        address=address,
        api_port=api_port,
        queue=queue,
        artifact_root=artifact_root,
        secret_path=secret_path,
        index_root=index_root,
        extra={"AETHER_DEFER_PROGRESS": "1"},
    )
    api_logs: list[str] = []
    first_logs: list[str] = []
    second_logs: list[str] = []
    api = _start([sys.executable, "-m", "aether_api.main"], env, api_logs)
    worker = _start([sys.executable, "-m", "aether_worker"], env, first_logs)
    resumed = None
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        _wait_line(first_logs, "aether-worker-ready", worker)
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=10) as http:
            owner_id, project_id, csrf = _bootstrap_project(http, secret)
            source_id = _create_source(http, csrf, project_id, worker, first_logs)
            queued, command_id = _queue_ingestion(http, csrf, project_id, source_id, worker, first_logs)
            assert queued["status"] == "QUEUED"
            database = Database(role_urls["aether_worker"])
            assert _command_status(database, owner_id, project_id, command_id) == "DELIVERED"
            _stop(worker)
            worker = None
            parked = http.get(f"/v1/projects/{project_id}/ingestions")
            assert parked.status_code == 200, parked.text
            assert parked.json()["items"][0]["status"] == "QUEUED", parked.text
            resumed_env = dict(env)
            resumed_env.pop("AETHER_DEFER_PROGRESS", None)
            resumed = _start([sys.executable, "-m", "aether_worker"], resumed_env, second_logs)
            _wait_line(second_logs, "aether-worker-ready", resumed)
            finished = _poll(
                http,
                f"/v1/projects/{project_id}/ingestions",
                lambda body: next((item for item in body["items"] if item["status"] in {"SUCCEEDED", "FAILED"}), None),
                resumed,
                second_logs,
            )
            assert finished["status"] == "SUCCEEDED", finished
            found = http.post(
                f"/v1/projects/{project_id}/search",
                headers=_headers(csrf, "sys01-restart-search"),
                json={"project_id": project_id, "query": "苹果派", "mode": "hybrid"},
            )
            assert found.status_code == 200, found.text + "".join(second_logs[-40:])
            assert found.json()["hits"][0]["text"] == APPLE
    finally:
        _stop(resumed)
        _stop(worker)
        _stop(api)
        secret_path.unlink(missing_ok=True)


def _bootstrap_project(http: httpx.Client, secret: str) -> tuple[str, str, str]:
    bootstrap = http.post(
        "/v1/auth/bootstrap",
        headers={"Origin": ORIGIN_VALUE},
        json={"bootstrap_secret": secret, "display_name": "所有者", "passphrase": PASSPHRASE},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    csrf = bootstrap.json()["csrf_token"]
    project = http.post("/v1/projects", headers=_headers(csrf, "sys01-restart-project"), json={"name": "重启"})
    assert project.status_code == 201, project.text
    return bootstrap.json()["owner_id"], project.json()["id"], csrf


def _create_source(http: httpx.Client, csrf: str, project_id: str, worker, logs: list[str]) -> str:
    source = http.post(
        f"/v1/projects/{project_id}/sources",
        headers=_headers(csrf, "sys01-restart-source"),
        json={"kind": "local_document", "locator": APPLE, "allowed_domains": ["example.test"]},
    )
    assert source.status_code == 202, source.text
    listed = _poll(
        http,
        f"/v1/projects/{project_id}/sources",
        lambda body: body["items"][0] if body["items"] else None,
        worker,
        logs,
    )
    return str(listed["id"])


def _queue_ingestion(http: httpx.Client, csrf: str, project_id: str, source_id: str, worker, logs: list[str]) -> tuple[dict, str]:
    ingested = http.post(
        f"/v1/projects/{project_id}/ingestions",
        headers=_headers(csrf, "sys01-restart-ingest"),
        json={"source_id": source_id},
    )
    assert ingested.status_code == 202, ingested.text

    def still_queued(body: dict) -> dict | None:
        item = next((row for row in body["items"] if row["source_id"] == source_id), None)
        if item is None:
            return None
        if item["status"] in {"SUCCEEDED", "FAILED", "RUNNING"}:
            raise AssertionError(f"采集在命令交付后没有停在 QUEUED: {item}")
        if item["status"] == "QUEUED":
            return item
        return None

    queued = _poll(http, f"/v1/projects/{project_id}/ingestions", still_queued, worker, logs)
    return queued, str(ingested.json()["command_id"])
