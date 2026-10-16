"""P0-02: one invalid command fails alone and leaves the worker loop running."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.console_commands import apply_console_commands
from aether_worker.service import route_pending
from aether_worker.skill_eval import record_evaluation
from tests.integration.conftest import insert_task
from tests.integration.system_audit import key, project, second_project, store_for

ROOT = Path(__file__).resolve().parents[2]


def _outbox(database: Database, owner: str, project_id: str) -> dict[str, str]:
    with database.transaction(owner_id=owner, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT command_key, status FROM command_outbox WHERE project_id = :p"),
            {"p": project_id},
        ).mappings().all()
    return {str(row["command_key"]): str(row["status"]) for row in found}


def _poison(client, project_id: str, headers: dict[str, str], variant: str) -> tuple[str, str]:
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

    error: BaseException | None = None
    try:
        route_pending(Database(role_urls["aether_dispatcher"]), Database(role_urls["aether_worker"]), Database(role_urls["aether_verifier"]), store_for(client, role_urls))
    except Exception as exc:
        error = exc
    statuses = _outbox(Database(role_urls["aether_worker"]), owner, project_id)
    other_status = _outbox(Database(role_urls["aether_worker"]), owner, other)
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


def test_documented_worker_survives_a_duplicate_source_and_keeps_running_tasks(client, role_urls) -> None:
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
        approved = False
        while time.time() < deadline:
            with worker.transaction(owner_id=owner, project_id=other) as conn:
                status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
            if status == "WAITING_APPROVAL" and not approved:
                shown = client.get(f"/v1/projects/{other}/tasks/{task_id}")
                digest = shown.json().get("contract_sha256") if shown.status_code == 200 else None
                if isinstance(digest, str) and len(digest) == 64:
                    sent = client.post(
                        f"/v1/projects/{other}/tasks/{task_id}/commands",
                        headers={**headers, "Idempotency-Key": key()},
                        json={"command_id": str(uuid.uuid4()), "kind": "approve_goal", "expected_contract_sha256": digest},
                    )
                    assert sent.status_code == 202, sent.text
                    approved = True
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
    assert first.status_code == 202, first.text
    assert later.status_code == 202, later.text
    error: BaseException | None = None
    try:
        route_pending(Database(role_urls["aether_dispatcher"]), Database(role_urls["aether_worker"]), Database(role_urls["aether_verifier"]), store_for(client, role_urls))
    except Exception as exc:
        error = exc
    statuses = _outbox(Database(role_urls["aether_worker"]), owner, project_id)
    detail = f"error={type(error).__name__ if error else None}: {error} first={statuses.get(first.json()['command_id'])} later={statuses.get(later.json()['command_id'])}"
    assert error is None, detail
    assert statuses[later.json()["command_id"]] == "DELIVERED", detail


def test_skill_rollback_without_a_pin_fails_only_that_command(client, role_urls) -> None:
    """S-007: a rollback that names a task with no skill pin does not roll back the rest of the batch."""
    owner, project_id, _headers = project(client)
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    stored = store.put(owner_id=owner, project_id=project_id, data=b"{}", media_type="application/json")
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        skill_id = str(
            conn.execute(
                text("INSERT INTO skills (project_id, name, description) VALUES (:project_id, :name, '说明') RETURNING id::text"),
                {"project_id": project_id, "name": f"skill-{uuid.uuid4().hex[:8]}"},
            ).scalar_one()
        )
        current_id = str(
            conn.execute(
                text(
                    "INSERT INTO skill_versions (project_id, skill_id, version, bundle_artifact_id, bundle_sha256, manifest, status) "
                    "VALUES (:project_id, :skill_id, 1, :artifact_id, :sha, '{}'::jsonb, 'ELIGIBLE') RETURNING id::text"
                ),
                {"project_id": project_id, "skill_id": skill_id, "artifact_id": stored["artifact_id"], "sha": stored["sha256"]},
            ).scalar_one()
        )
        previous = store.put(owner_id=owner, project_id=project_id, data=b"{\"v\":2}", media_type="application/json")
        previous_id = str(
            conn.execute(
                text(
                    "INSERT INTO skill_versions (project_id, skill_id, version, bundle_artifact_id, bundle_sha256, manifest, status) "
                    "VALUES (:project_id, :skill_id, 2, :artifact_id, :sha, '{}'::jsonb, 'ACTIVE') RETURNING id::text"
                ),
                {"project_id": project_id, "skill_id": skill_id, "artifact_id": previous["artifact_id"], "sha": previous["sha256"]},
            ).scalar_one()
        )
        pinned_task = insert_task(worker, owner, project_id, "已固定版本")
        bare_task = insert_task(worker, owner, project_id, "没有技能钉")
        approval_id = str(
            conn.execute(
                text(
                    "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, decision, owner_id, expires_at, decided_at) "
                    "VALUES (:project_id, :task_id, :digest, '{}'::jsonb, 'approved', :owner, :expires, now()) RETURNING id::text"
                ),
                {
                    "project_id": project_id,
                    "task_id": pinned_task,
                    "digest": "a" * 64,
                    "owner": owner,
                    "expires": datetime.now(UTC) + timedelta(hours=1),
                },
            ).scalar_one()
        )
    evaluation_id = record_evaluation(
        Database(role_urls["aether_verifier"]),
        owner_id=owner,
        project_id=project_id,
        candidate_version_id=current_id,
        dataset_sha256="a" * 64,
        purpose="development",
        split="train",
        metrics={"families": 4, "ci_lower": 0.2, "success_delta": 0.2, "cost_ratio": 1},
        synthetic=True,
    )
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO skill_deployments (project_id, skill_id, slot, version_id, evaluation_id, approval_id, deployment_version, traffic_fraction) "
                "VALUES (:project_id, :skill_id, 'canary', :version_id, :evaluation_id, :approval_id, 1, 0.05)"
            ),
            {
                "project_id": project_id,
                "skill_id": skill_id,
                "version_id": current_id,
                "evaluation_id": evaluation_id,
                "approval_id": approval_id,
            },
        )
        conn.execute(text("UPDATE skill_versions SET status = 'CANARY' WHERE project_id = :project_id AND id = :id"), {"project_id": project_id, "id": current_id})
        rollback_key = f"rollback-{uuid.uuid4().hex}"
        source_key = f"source-{uuid.uuid4().hex}"
        rollback_payload = {
            "action": "rollback",
            "skill_id": skill_id,
            "target_version_id": previous_id,
            "task_id": bare_task,
            "command_id": rollback_key,
        }
        locator = "https://example.test/after-rollback"
        source_payload = {
            "source_id": str(uuid.uuid4()),
            "kind": "url",
            "locator": locator,
            "canonical_locator_hash": sha256_text(f"url\n{locator}"),
            "allowed_domains": ["example.test"],
        }
        for command_key, kind, payload in (
            (rollback_key, "skill.deploy", rollback_payload),
            (source_key, "source.create", source_payload),
        ):
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            conn.execute(
                text(
                    "INSERT INTO command_outbox (project_id, command_key, kind, payload, payload_sha256) "
                    "VALUES (:project_id, :command_key, :kind, CAST(:payload AS jsonb), :digest)"
                ),
                {
                    "project_id": project_id,
                    "command_key": command_key,
                    "kind": kind,
                    "payload": encoded,
                    "digest": sha256_text(encoded),
                },
            )
    apply_console_commands(worker, owner_id=owner, project_id=project_id)
    statuses = _outbox(worker, owner, project_id)
    detail = f"rollback={statuses.get(rollback_key)} source={statuses.get(source_key)}"
    assert statuses[rollback_key] == "FAILED", detail
    assert statuses[source_key] == "DELIVERED", detail
