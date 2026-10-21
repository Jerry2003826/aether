"""Codex review follow-ups for coding jobs and skill evaluation."""

from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.coding_sandbox import coding_container_ids, release_coding_container
from aether_worker.console_commands import apply_console_commands
from aether_worker.runner import docker_available
from aether_worker.service import route_pending
from tests.integration.test_console_records import _headers, _session

CANDIDATE = {
    "kind": "aether.skill_candidate",
    "files": {"candidate.py": "def value():\n    return 42\n"},
}
DATASET = {
    "kind": "aether.skill_dataset",
    "split": "development",
    "synthetic": False,
    "contract": "返回四十二",
    "baseline_passes": 0,
    "baseline_cost": 1,
    "candidate_cost": 1,
    "cases": [
        {
            "family": "value",
            "suite_files": {"test_value.py": "from candidate import value\n\ndef test_value():\n    assert value() == 42\n"},
        }
    ],
}
WORKSPACE = {
    "kind": "aether.coding_workspace",
    "base_files": {"app.py": "def value():\n    return 0\n"},
    "files": {"app.py": "def value():\n    return 1\n"},
    "tests": {
        "test_app.py": "async def test_value():\n    assert False\n\ndef test_print():\n    print('X' * 100000)\n"
    },
}
SLEEP_WORKSPACE = {
    "kind": "aether.coding_workspace",
    "base_files": {"app.py": "def value():\n    return 0\n"},
    "files": {"app.py": "def value():\n    return 1\n"},
    "tests": {"test_app.py": "import time\n\ndef test_wait():\n    time.sleep(15)\n"},
}


def _require_docker() -> None:
    if not docker_available():
        pytest.fail("Docker is required for the coding-job review checks")


def _upload(client, project_id: str, csrf: str, key: str, name: str, body: dict) -> str:
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers=_headers(csrf, key),
        files={"file": (name, json.dumps(body).encode(), "application/json")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return str(uploaded.json()["artifact_id"])


def _task(client, project_id: str, csrf: str, key: str) -> str:
    created = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers=_headers(csrf, key),
        json={"goal": "审查回归"},
    )
    assert created.status_code == 202, created.text
    return str(created.json()["resource_id"])


def _import_skill(client, project_id: str, csrf: str, key: str, bundle_id: str) -> str:
    imported = client.post(
        f"/v1/projects/{project_id}/skills",
        headers=_headers(csrf, key),
        json={"bundle_artifact_id": bundle_id},
    )
    assert imported.status_code == 202, imported.text
    return str(imported.json()["resource_id"])


def _version_id(database: Database, owner_id: str, project_id: str, skill_id: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT id::text FROM skill_versions WHERE project_id = :project_id AND skill_id = :skill_id"),
            {"project_id": project_id, "skill_id": skill_id},
        ).scalar_one()
    return str(found)


def _route(client, role_urls, owner_id: str, project_id: str) -> None:
    worker = Database(role_urls["aether_worker"])
    route_pending(
        Database(role_urls["aether_dispatcher"]),
        worker,
        Database(role_urls["aether_verifier"]),
        ArtifactStore(client.app.state.settings.artifact_root, worker),
    )


def test_duplicate_job_key_is_one_accepted_command(client, role_urls) -> None:
    owner_id, project_id, csrf = _session(client, "r7-dup-project")
    task_id = _task(client, project_id, csrf, "r7-dup-task")
    artifact_id = _upload(client, project_id, csrf, "r7-dup-file", "base.json", WORKSPACE)
    body = {"task_id": task_id, "base_artifact_id": artifact_id, "job_key": "same", "objective": "只受理一次"}
    first = client.post(f"/v1/projects/{project_id}/coding-jobs", headers=_headers(csrf, "r7-dup-key-a"), json=body)
    second = client.post(f"/v1/projects/{project_id}/coding-jobs", headers=_headers(csrf, "r7-dup-key-b"), json=body)
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["resource_id"] == second.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        pending = conn.execute(
            text(
                "SELECT count(*) FROM command_outbox "
                "WHERE project_id = :project_id AND kind = 'coding.create' AND status = 'PENDING'"
            ),
            {"project_id": project_id},
        ).scalar_one()
    assert int(pending) == 1


def test_unrelated_baseline_is_not_accepted(client, role_urls) -> None:
    owner_id, project_id, csrf = _session(client, "r7-base-project")
    bundle = _upload(client, project_id, csrf, "r7-base-bundle", "candidate.json", CANDIDATE)
    other = _upload(client, project_id, csrf, "r7-base-other", "other.json", CANDIDATE)
    first = _import_skill(client, project_id, csrf, "r7-base-import-a", bundle)
    second = _import_skill(client, project_id, csrf, "r7-base-import-b", other)
    worker = Database(role_urls["aether_worker"])
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    candidate = _version_id(worker, owner_id, project_id, first)
    baseline = _version_id(worker, owner_id, project_id, second)
    dataset = _upload(client, project_id, csrf, "r7-base-data", "dataset.json", DATASET)
    missing = client.post(
        f"/v1/projects/{project_id}/skills/{first}/evaluate",
        headers=_headers(csrf, "r7-base-missing"),
        json={
            "candidate_version_id": candidate,
            "baseline_version_id": str(uuid.uuid4()),
            "dataset_manifest_id": dataset,
            "budget": {"max_tokens": 10},
        },
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["code"] == "not_found"
    unrelated = client.post(
        f"/v1/projects/{project_id}/skills/{first}/evaluate",
        headers=_headers(csrf, "r7-base-unrelated"),
        json={
            "candidate_version_id": candidate,
            "baseline_version_id": baseline,
            "dataset_manifest_id": dataset,
            "budget": {"max_tokens": 10},
        },
    )
    assert unrelated.status_code == 404, unrelated.text
    assert unrelated.json()["code"] == "not_found"
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        queued = conn.execute(
            text("SELECT count(*) FROM command_outbox WHERE project_id = :project_id AND kind = 'skill.evaluate'"),
            {"project_id": project_id},
        ).scalar_one()
    assert int(queued) == 0


def test_active_seconds_zero_rejects_before_a_grade(client, role_urls) -> None:
    owner_id, project_id, csrf = _session(client, "r7-dead-project")
    bundle = _upload(client, project_id, csrf, "r7-dead-bundle", "candidate.json", CANDIDATE)
    skill_id = _import_skill(client, project_id, csrf, "r7-dead-import", bundle)
    worker = Database(role_urls["aether_worker"])
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    version_id = _version_id(worker, owner_id, project_id, skill_id)
    dataset = _upload(client, project_id, csrf, "r7-dead-data", "dataset.json", DATASET)
    queued = client.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/evaluate",
        headers=_headers(csrf, "r7-dead-eval"),
        json={
            "candidate_version_id": version_id,
            "dataset_manifest_id": dataset,
            "budget": {"max_tokens": 10, "active_seconds": 0},
        },
    )
    assert queued.status_code == 202, queued.text
    started = time.monotonic()
    _route(client, role_urls, owner_id, project_id)
    assert time.monotonic() - started < 20
    listed = client.get(f"/v1/projects/{project_id}/evaluations")
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["status"] == "REJECTED"
    assert item["metrics"] is None
    assert item["configuration"]["error_code"] == "deadline_exceeded"


def test_canary_and_baseline_survive_evaluation(client, role_urls) -> None:
    _require_docker()
    owner_id, project_id, csrf = _session(client, "r7-canary-project")
    bundle = _upload(client, project_id, csrf, "r7-canary-bundle", "candidate.json", CANDIDATE)
    skill_id = _import_skill(client, project_id, csrf, "r7-canary-import", bundle)
    worker = Database(role_urls["aether_worker"])
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    version_id = _version_id(worker, owner_id, project_id, skill_id)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE skill_versions SET status = 'CANARY', "
                "manifest = jsonb_set(manifest, '{status}', to_jsonb(CAST('CANARY' AS text)), true) "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": version_id},
        )
    dataset = _upload(client, project_id, csrf, "r7-canary-data", "dataset.json", DATASET)
    queued = client.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/evaluate",
        headers=_headers(csrf, "r7-canary-eval"),
        json={
            "candidate_version_id": version_id,
            "baseline_version_id": version_id,
            "dataset_manifest_id": dataset,
            "budget": {"max_tokens": 10},
        },
    )
    assert queued.status_code == 202, queued.text
    _route(client, role_urls, owner_id, project_id)
    listed = client.get(f"/v1/projects/{project_id}/skills")
    assert listed.json()["items"][0]["versions"][0]["status"] == "CANARY"
    evaluations = client.get(f"/v1/projects/{project_id}/evaluations")
    item = evaluations.json()["items"][0]
    assert item["status"] == "INSUFFICIENT_EVIDENCE"
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        baseline = conn.execute(
            text("SELECT baseline_version_id::text FROM evaluation_runs WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": queued.json()["resource_id"]},
        ).scalar_one()
        status = conn.execute(
            text("SELECT status FROM skill_versions WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": version_id},
        ).scalar_one()
    assert str(baseline) == version_id
    assert status == "CANARY"


def test_slow_grader_stops_when_active_seconds_expire(client, role_urls) -> None:
    _require_docker()
    owner_id, project_id, csrf = _session(client, "r7-slow-project")
    bundle = _upload(client, project_id, csrf, "r7-slow-bundle", "candidate.json", CANDIDATE)
    skill_id = _import_skill(client, project_id, csrf, "r7-slow-import", bundle)
    worker = Database(role_urls["aether_worker"])
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    version_id = _version_id(worker, owner_id, project_id, skill_id)
    slow = dict(DATASET)
    slow["cases"] = [
        {
            "family": "slow",
            "suite_files": {"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(30)\n"},
        }
    ]
    dataset = _upload(client, project_id, csrf, "r7-slow-data", "dataset.json", slow)
    queued = client.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/evaluate",
        headers=_headers(csrf, "r7-slow-eval"),
        json={
            "candidate_version_id": version_id,
            "dataset_manifest_id": dataset,
            "budget": {"max_tokens": 10, "active_seconds": 2},
        },
    )
    assert queued.status_code == 202, queued.text
    started = time.monotonic()
    _route(client, role_urls, owner_id, project_id)
    assert time.monotonic() - started < 90
    item = client.get(f"/v1/projects/{project_id}/evaluations").json()["items"][0]
    assert item["status"] == "REJECTED"
    assert item["configuration"]["error_code"] == "deadline_exceeded"


def _job_state(role_urls, owner_id: str, project_id: str, job_id: str) -> str:
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return str(
            conn.execute(
                text("SELECT state FROM coding_jobs WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": job_id},
            ).scalar_one()
        )


def _route_until_terminal(client, role_urls, owner_id: str, project_id: str, job_id: str) -> str:
    _route(client, role_urls, owner_id, project_id)
    return _job_state(role_urls, owner_id, project_id, job_id)


def _async_failure(client, role_urls, prefix: str) -> tuple[str, str, str, str]:
    _require_docker()
    owner_id, project_id, csrf = _session(client, f"{prefix}-project")
    task_id = _task(client, project_id, csrf, f"{prefix}-task")
    artifact_id = _upload(client, project_id, csrf, f"{prefix}-file", "async.json", WORKSPACE)
    created = client.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, f"{prefix}-job"),
        json={"task_id": task_id, "base_artifact_id": artifact_id, "job_key": "async", "objective": "不要把协程当成通过"},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["resource_id"]
    assert _route_until_terminal(client, role_urls, owner_id, project_id, job_id) == "FAILED"
    return owner_id, project_id, task_id, job_id


def test_an_attempt_that_loses_its_lease_is_finished_by_the_next_one(client, role_urls, monkeypatch) -> None:
    # Under load the first attempt can lose its lease (coding_lease_lost_during_drain, r11 565299d run 1):
    # the job stays ACTIVE with fence_lost until the lease runs out and the next route claims it. The
    # test must wait for that terminal state, bounded by the lease, not read one snapshot.
    from aether_worker import coding_jobs

    real_renew = coding_jobs._renew_lease
    lost: list[int] = []

    def lose_once(*args, **kwargs):
        if not lost:
            lost.append(1)
            return False
        return real_renew(*args, **kwargs)

    monkeypatch.setattr(coding_jobs, "_renew_lease", lose_once)
    _async_failure(client, role_urls, "r7-lease")
    assert lost == [1]


def test_async_failure_is_visible_on_the_task_stream(client, role_urls) -> None:
    owner_id, project_id, task_id, job_id = _async_failure(client, role_urls, "r7-async")
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        result_id = conn.execute(
            text("SELECT result_artifact_id::text FROM coding_jobs WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": job_id},
        ).scalar_one()
    result, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=str(result_id))
    assert b'"detail":"async"' in result
    events = client.get(f"/v1/projects/{project_id}/tasks/{task_id}/events")
    assert events.status_code == 200, events.text
    assert "coding_job.terminal" in events.text
    assert job_id in events.text
    assert '"state": "FAILED"' in events.text or '"state":"FAILED"' in events.text


def test_stale_attempt_cannot_overwrite_a_requeued_job(client, role_urls) -> None:
    _require_docker()
    owner_id, project_id, csrf = _session(client, "r7-fence-project")
    task_id = _task(client, project_id, csrf, "r7-fence-task")
    artifact_id = _upload(client, project_id, csrf, "r7-fence-file", "sleep.json", SLEEP_WORKSPACE)
    created = client.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "r7-fence-job"),
        json={"task_id": task_id, "base_artifact_id": artifact_id, "job_key": "fence", "objective": "过期尝试不能收尾"},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    error: list[BaseException] = []
    fencing = 0

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
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "UPDATE coding_jobs SET fencing_token = fencing_token + 1, state = 'QUEUED' "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"project_id": project_id, "id": job_id},
            )
            fencing = conn.execute(
                text("SELECT fencing_token FROM coding_jobs WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": job_id},
            ).scalar_one()
        thread.join(40)
    finally:
        release_coding_container(job_id)
        thread.join(5)
    assert error == [], error
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT state, fencing_token, patch_artifact_id::text AS patch_id "
                "FROM coding_jobs WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": job_id},
        ).mappings().one()
    assert row["state"] == "QUEUED"
    assert int(row["fencing_token"]) == int(fencing)
    assert row["patch_id"] is None
