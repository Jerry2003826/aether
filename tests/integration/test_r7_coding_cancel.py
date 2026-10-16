"""Coding-job cancel is confirmed only after the container is gone. A live lease is not reaped."""

from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.coding_jobs import reap_expired_coding_jobs
from aether_worker.coding_sandbox import coding_container_ids, container_name, release_coding_container
from aether_worker.console_commands import apply_console_commands
from aether_worker.runner import _docker, docker_available
from aether_worker.service import route_pending
from tests.integration.test_console_records import _headers, _session

SLEEP_WORKSPACE = {
    "kind": "aether.coding_workspace",
    "base_files": {"app.py": "def value():\n    return 0\n"},
    "files": {"app.py": "def value():\n    return 1\n"},
    "tests": {"test_app.py": "import time\n\ndef test_wait():\n    time.sleep(30)\n"},
}


def _require_docker() -> None:
    if not docker_available():
        pytest.fail("Docker is required for coding-job cancel and TTL")


def test_cancel_is_cancelled_only_after_the_container_is_gone(client, role_urls) -> None:
    _require_docker()
    owner_id, project_id, csrf = _session(client, "r7-cancel-project")
    task = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers=_headers(csrf, "r7-cancel-task"),
        json={"goal": "取消正在跑的编码作业"},
    )
    assert task.status_code == 202, task.text
    task_id = task.json()["resource_id"]
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers=_headers(csrf, "r7-cancel-file"),
        files={"file": ("sleep.json", json.dumps(SLEEP_WORKSPACE).encode(), "application/json")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "r7-cancel-job"),
        json={
            "task_id": task_id,
            "base_artifact_id": uploaded.json()["artifact_id"],
            "job_key": "sleep",
            "objective": "跑到一半取消",
        },
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    error: list[BaseException] = []

    def _run() -> None:
        try:
            route_pending(
                Database(role_urls["aether_dispatcher"]),
                worker,
                Database(role_urls["aether_verifier"]),
                store,
            )
        except BaseException as exc:  # noqa: BLE001 - the thread must report the worker failure
            error.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        deadline = time.time() + 40
        while time.time() < deadline and not coding_container_ids(job_id):
            time.sleep(0.2)
        assert coding_container_ids(job_id), "编码容器没有起来"
        cancelled = client.post(
            f"/v1/projects/{project_id}/tasks/{task_id}/commands",
            headers=_headers(csrf, "r7-cancel-cmd"),
            json={"command_id": str(uuid.uuid4()), "kind": "cancel"},
        )
        assert cancelled.status_code == 202, cancelled.text
        thread.join(60)
    finally:
        release_coding_container(job_id)
        thread.join(5)
    assert error == [], error
    assert thread.is_alive() is False
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT state, events_artifact_id::text AS events_id, patch_artifact_id::text AS patch_id "
                "FROM coding_jobs WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": job_id},
        ).mappings().one()
        grant = conn.execute(
            text(
                "SELECT g.revoked_at FROM capability_grants g "
                "JOIN operations o ON o.project_id = g.project_id AND o.capability_id = g.id "
                "JOIN coding_jobs j ON j.project_id = o.project_id AND j.operation_id = o.id "
                "WHERE j.project_id = :project_id AND j.id = :id"
            ),
            {"project_id": project_id, "id": job_id},
        ).scalar_one()
        kept = conn.execute(
            text("SELECT count(*) FROM coding_jobs WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": job_id},
        ).scalar_one()
    assert row["state"] == "CANCELLED"
    assert int(kept) == 1
    assert grant is not None
    assert coding_container_ids(job_id) == []
    events, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=row["events_id"])
    body = json.loads(events)
    assert body["cancel_state"] == "confirmed"
    assert body["external_effect"] == "unknown"
    assert isinstance(body["file_manifest"], list) and body["file_manifest"]
    assert len(body["patch_sha256"]) == 64
    assert len(body["stdout_tail"]) <= 4096
    assert len(body["stderr_tail"]) <= 4096
    patch, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=row["patch_id"])
    assert b"return 1" in patch


def test_reaper_does_not_remove_a_leased_job(client, role_urls) -> None:
    _require_docker()
    owner_id, project_id, csrf = _session(client, "r7-lease-project")
    task = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers=_headers(csrf, "r7-lease-task"),
        json={"goal": "租约保护活跃作业"},
    )
    assert task.status_code == 202, task.text
    task_id = task.json()["resource_id"]
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers=_headers(csrf, "r7-lease-file"),
        files={"file": ("base.json", json.dumps(SLEEP_WORKSPACE).encode(), "application/json")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "r7-lease-job"),
        json={
            "task_id": task_id,
            "base_artifact_id": uploaded.json()["artifact_id"],
            "job_key": "lease",
            "objective": "不要删掉还活着的作业",
        },
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id, store=store)
    name = container_name(job_id)
    started = _docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"aether.coding={job_id}",
            "--label",
            "aether.role=coding",
            "--network",
            "none",
            "python:3.12-alpine",
            "sleep",
            "120",
        ]
    )
    assert started.returncode == 0, started.stderr
    try:
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "UPDATE coding_jobs SET deadline = now() - interval '1 minute' "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"project_id": project_id, "id": job_id},
            )
            conn.execute(
                text(
                    "UPDATE operations SET lease_until = now() + interval '5 minutes' "
                    "WHERE project_id = :project_id AND id = (SELECT operation_id FROM coding_jobs WHERE project_id = :project_id AND id = :id)"
                ),
                {"project_id": project_id, "id": job_id},
            )
        reap_expired_coding_jobs(worker, store, owner_id=owner_id, project_id=project_id)
        running = _docker(["inspect", "-f", "{{.State.Running}}", name])
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            state = conn.execute(
                text("SELECT state FROM coding_jobs WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": job_id},
            ).scalar_one()
        assert running.stdout.strip() == "true"
        assert state == "QUEUED"
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "UPDATE operations SET lease_until = now() - interval '1 minute' "
                    "WHERE project_id = :project_id AND id = (SELECT operation_id FROM coding_jobs WHERE project_id = :project_id AND id = :id)"
                ),
                {"project_id": project_id, "id": job_id},
            )
        reap_expired_coding_jobs(worker, store, owner_id=owner_id, project_id=project_id)
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT j.state, o.error_code FROM coding_jobs j "
                    "JOIN operations o ON o.project_id = j.project_id AND o.id = j.operation_id "
                    "WHERE j.project_id = :project_id AND j.id = :id"
                ),
                {"project_id": project_id, "id": job_id},
            ).mappings().one()
            kept = conn.execute(
                text("SELECT count(*) FROM coding_jobs WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": job_id},
            ).scalar_one()
        assert row["state"] == "FAILED"
        assert row["error_code"] == "deadline_exceeded"
        assert int(kept) == 1
        assert coding_container_ids(job_id) == []
    finally:
        _docker(["rm", "-f", name])
