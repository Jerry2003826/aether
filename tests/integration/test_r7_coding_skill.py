"""Public coding jobs and skill evaluations reach a worker terminal state."""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text

from aether_api.auth_service import issue_bootstrap_secret
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
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


def _workspace(returned: int, expected: int) -> bytes:
    body = {
        "kind": "aether.coding_workspace",
        "base_files": {"app.py": "def value():\n    return 0\n"},
        "files": {"app.py": f"def value():\n    return {returned}\n"},
        "tests": {"test_app.py": f"import app\n\ndef test_value():\n    assert app.value() == {expected}\n"},
    }
    return json.dumps(body).encode()


def _dataset(*, baseline_cost: int, candidate_cost: int) -> bytes:
    body = {
        "kind": "aether.skill_dataset",
        "split": "development",
        "synthetic": False,
        "contract": "返回四十二",
        "baseline_passes": 0,
        "baseline_cost": baseline_cost,
        "candidate_cost": candidate_cost,
        "cases": [
            {
                "family": "value",
                "suite_files": {
                    "test_value.py": "from candidate import value\n\ndef test_value():\n    assert value() == 42\n"
                },
            }
        ],
    }
    return json.dumps(body).encode()


def _candidate() -> bytes:
    return json.dumps(
        {"kind": "aether.skill_candidate", "files": {"candidate.py": "def value():\n    return 42\n"}},
        ensure_ascii=False,
    ).encode()


def _wait_job(database: Database, owner_id: str, project_id: str, job_key: str, states: set[str], proc, logs: list[str]) -> dict:
    deadline = time.time() + 180
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"worker 退出 {proc.returncode}: {''.join(logs[-60:])}")
        with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT state, fencing_token, patch_artifact_id::text AS patch_id, "
                    "result_artifact_id::text AS result_id, events_artifact_id::text AS events_id "
                    "FROM coding_jobs WHERE project_id = :project_id AND job_key = :job_key"
                ),
                {"project_id": project_id, "job_key": job_key},
            ).mappings().first()
        if row is not None and str(row["state"]) in states:
            return dict(row)
        last = "" if row is None else str(row["state"])
        time.sleep(0.3)
    raise AssertionError(f"编码作业停在 {last}: {''.join(logs[-60:])}")


def _wait_evaluation(http: httpx.Client, project_id: str, evaluation_id: str, proc, logs: list[str]) -> dict:
    deadline = time.time() + 180
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"worker 退出 {proc.returncode}: {''.join(logs[-60:])}")
        response = http.get(f"/v1/projects/{project_id}/evaluations")
        last = response.text
        if response.status_code == 200:
            found = next((item for item in response.json()["items"] if item["id"] == evaluation_id), None)
            if found is not None and found["status"] != "QUEUED":
                return found
        time.sleep(0.4)
    raise AssertionError(last + "".join(logs[-60:]))


def test_public_coding_job_and_skill_eval_reach_terminal_states(role_urls, clean_db, tmp_path: Path) -> None:
    address = _temporal_address()
    api_port = _port()
    queue = f"aether-r7-{uuid.uuid4()}"
    artifact_root = tmp_path / "artifacts"
    secret_path = ROOT / "secrets" / f"r7-{uuid.uuid4().hex}.json"
    secret = issue_bootstrap_secret(secret_path)
    env = _service_env(
        role_urls,
        address=address,
        api_port=api_port,
        queue=queue,
        artifact_root=artifact_root,
        secret_path=secret_path,
        index_root=tmp_path / "indexes",
    )
    api_logs: list[str] = []
    worker_logs: list[str] = []
    api = _start([sys.executable, "-m", "aether_api.main"], env, api_logs)
    worker = _start([sys.executable, "-m", "aether_worker"], env, worker_logs)
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        _wait_line(worker_logs, "aether-worker-ready", worker)
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=30) as http:
            _exercise(http, role_urls, worker, worker_logs, secret, artifact_root)
    finally:
        _stop(worker)
        _stop(api)
        secret_path.unlink(missing_ok=True)


def _exercise(http: httpx.Client, role_urls, proc, logs: list[str], secret: str, artifact_root: Path) -> None:
    bootstrap = http.post(
        "/v1/auth/bootstrap",
        headers={"Origin": ORIGIN_VALUE},
        json={"bootstrap_secret": secret, "display_name": "所有者", "passphrase": PASSPHRASE},
    )
    assert bootstrap.status_code == 201, bootstrap.text
    csrf = bootstrap.json()["csrf_token"]
    owner_id = bootstrap.json()["owner_id"]
    project = http.post("/v1/projects", headers=_headers(csrf, "r7-project"), json={"name": "编码与评测"})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    task = http.post(
        f"/v1/projects/{project_id}/tasks",
        headers=_headers(csrf, "r7-task-create"),
        json={"goal": "让 value 返回 42"},
    )
    assert task.status_code == 202, task.text
    task_id = task.json()["resource_id"]
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(artifact_root, database)
    _coding(http, csrf, owner_id, project_id, task_id, database, store, proc, logs)
    _skill(http, csrf, owner_id, project_id, database, proc, logs)


def _upload(http: httpx.Client, csrf: str, project_id: str, key: str, name: str, data: bytes, media: str) -> str:
    uploaded = http.post(
        f"/v1/projects/{project_id}/artifacts",
        headers=_headers(csrf, key),
        files={"file": (name, data, media)},
    )
    assert uploaded.status_code == 201, uploaded.text
    return str(uploaded.json()["artifact_id"])


def _task_status(database: Database, owner_id: str, project_id: str, task_id: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        status = conn.execute(
            text("SELECT status FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).scalar_one()
    return str(status)


def _wait_approval(database: Database, owner_id: str, project_id: str, task_id: str, proc, logs: list[str]) -> None:
    deadline = time.time() + 60
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"worker 退出 {proc.returncode}: {''.join(logs[-60:])}")
        last = _task_status(database, owner_id, project_id, task_id)
        if last == "RUNNING":
            raise AssertionError("任务在编码作业开始前已是 RUNNING")
        if last == "WAITING_APPROVAL":
            return
        time.sleep(0.2)
    raise AssertionError(f"任务没有停在 WAITING_APPROVAL，最后是 {last}")


def _coding(http, csrf, owner_id, project_id, task_id, database, store, proc, logs) -> None:
    _wait_approval(database, owner_id, project_id, task_id, proc, logs)
    # The no-upstream model really edits app.py from return 0 to return 42 (SCRIPTED_EDIT_COMMAND).
    # The first attempt's tests want 43, so the edited code still fails; the retry's tests want 42.
    broken = _upload(http, csrf, project_id, "r7-code-broken", "broken.json", _workspace(0, 43), "application/json")
    failed = http.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "r7-code-fail"),
        json={"task_id": task_id, "base_artifact_id": broken, "job_key": "value", "objective": "先失败再重试"},
    )
    assert failed.status_code == 202, failed.text
    job_id = failed.json()["resource_id"]
    first = _wait_job(database, owner_id, project_id, "value", {"FAILED"}, proc, logs)
    assert first["patch_id"]
    assert _task_status(database, owner_id, project_id, task_id) == "WAITING_APPROVAL"
    fixed = _upload(http, csrf, project_id, "r7-code-fixed", "fixed.json", _workspace(0, 42), "application/json")
    again = http.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "r7-code-retry"),
        json={"task_id": task_id, "base_artifact_id": fixed, "job_key": "value", "objective": "改成四十二"},
    )
    assert again.status_code == 202, again.text
    assert again.json()["resource_id"] == job_id
    finished = _wait_job(database, owner_id, project_id, "value", {"FINISHED"}, proc, logs)
    assert int(finished["fencing_token"]) >= 1
    patch, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=finished["patch_id"])
    events, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=finished["events_id"])
    assert b"return 42" in patch
    event = json.loads(events)
    assert event["network"] == "none"
    assert event["read_only"] is True
    assert event["network_errno"] not in {0, None}
    assert event["credential_present"] is False
    visible = http.get(f"/v1/projects/{project_id}/tasks/{task_id}")
    assert visible.status_code == 200, visible.text
    body = visible.json()
    assert finished["patch_id"] in body["artifact_ids"]
    assert finished["result_id"] in body["artifact_ids"]
    assert body["budget"]["request_count"] >= 2
    assert body["budget"]["held_micro_usd"] == 0
    assert _task_status(database, owner_id, project_id, task_id) == "WAITING_APPROVAL"


def _skill(http, csrf, owner_id, project_id, database, proc, logs) -> None:
    bundle = _upload(http, csrf, project_id, "r7-skill-bundle", "candidate.json", _candidate(), "application/json")
    imported = http.post(
        f"/v1/projects/{project_id}/skills",
        headers=_headers(csrf, "r7-skill-import"),
        json={"bundle_artifact_id": bundle},
    )
    assert imported.status_code == 202, imported.text
    skill_id = imported.json()["resource_id"]
    deadline = time.time() + 30
    version_id = None
    while time.time() < deadline and version_id is None:
        with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            version_id = conn.execute(
                text("SELECT id::text FROM skill_versions WHERE project_id = :project_id AND skill_id = :skill_id"),
                {"project_id": project_id, "skill_id": skill_id},
            ).scalar()
        time.sleep(0.2)
    assert version_id
    dataset = _upload(http, csrf, project_id, "r7-skill-data", "dataset.json", _dataset(baseline_cost=1, candidate_cost=1), "application/json")
    queued = http.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/evaluate",
        headers=_headers(csrf, "r7-skill-eval"),
        json={"candidate_version_id": version_id, "dataset_manifest_id": dataset, "budget": {"max_tokens": 10}},
    )
    assert queued.status_code == 202, queued.text
    evaluation = _wait_evaluation(http, project_id, queued.json()["resource_id"], proc, logs)
    assert evaluation["status"] == "INSUFFICIENT_EVIDENCE"
    assert evaluation["metrics"]["families"] == 1
    assert evaluation["metrics"]["grade_passes"] == 1
    assert evaluation["report_artifact_id"]
    listed = http.get(f"/v1/projects/{project_id}/skills")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["versions"][0]["status"] == "EVALUATING"
    poisoned = _upload(
        http,
        csrf,
        project_id,
        "r7-skill-poison",
        "poison.json",
        _dataset(baseline_cost=0, candidate_cost=1),
        "application/json",
    )
    rejected = http.post(
        f"/v1/projects/{project_id}/skills/{skill_id}/evaluate",
        headers=_headers(csrf, "r7-skill-nan"),
        json={"candidate_version_id": version_id, "dataset_manifest_id": poisoned, "budget": {"max_tokens": 10}},
    )
    assert rejected.status_code == 202, rejected.text
    blocked = _wait_evaluation(http, project_id, rejected.json()["resource_id"], proc, logs)
    assert blocked["status"] == "REJECTED"
    assert blocked["metrics"] is None
    assert blocked["configuration"]["error_code"] == "non_finite"
    assert "Infinity" not in json.dumps(blocked)
    assert "NaN" not in json.dumps(blocked)
