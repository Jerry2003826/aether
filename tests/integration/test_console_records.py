import json
from pathlib import Path

from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.console_commands import apply_console_commands
from aether_worker.memory import apply_memory_commands
from aether_worker.service import route_pending
from tests.integration.fake_coding_upstream import PROVIDER_KEY, FakeCodingModel, editing_upstream
from tests.integration.test_auth import ORIGIN, _bootstrap

CONSOLE = Path("apps/web/app/console/page.tsx").read_text(encoding="utf-8")
CODE = Path("apps/web/app/code/page.tsx").read_text(encoding="utf-8")
TASK_PATH = "/v1/projects/${projectId}/tasks"
MEMORY_PATH = "/v1/projects/${projectId}/memories"
CODING_PATH = "/v1/projects/${projectId}/coding-jobs"


def _session(client, key: str):
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    headers = {**ORIGIN, "X-CSRF-Token": body["csrf_token"], "Idempotency-Key": key}
    project = client.post("/v1/projects", headers=headers, json={"name": "同一条记录"})
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"], body["csrf_token"]


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {**ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key}


def test_console_task_create_matches_the_database(client, role_urls) -> None:
    assert TASK_PATH in CONSOLE
    owner_id, project_id, csrf = _session(client, "console-record-task-project")
    goal = "核对锁文件里的版本"
    created = client.post(f"/v1/projects/{project_id}/tasks", headers=_headers(csrf, "console-record-task"), json={"goal": goal})
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT id::text AS id, original_goal, status FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).mappings().one()
    assert row["id"] == task_id
    assert row["original_goal"] == goal
    assert row["status"] == "QUEUED"


def test_console_memory_create_matches_the_database(client, role_urls, tmp_path: Path) -> None:
    assert MEMORY_PATH in CONSOLE
    owner_id, project_id, csrf = _session(client, "console-record-memory-project")
    claim = "锁文件必须和合同哈希一致"
    created = client.post(
        f"/v1/projects/{project_id}/memories",
        headers=_headers(csrf, "console-record-memory"),
        json={"claim": claim, "kind": "semantic", "category": "fact"},
    )
    assert created.status_code == 202, created.text
    memory_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    apply_memory_commands(worker, ArtifactStore(tmp_path / "artifacts", worker), owner_id=owner_id, project_id=project_id)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT id::text AS id, claim, kind, category, epistemic_status FROM memories WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": memory_id},
        ).mappings().one()
    assert row["id"] == memory_id
    assert row["claim"] == claim
    assert row["kind"] == "semantic"
    assert row["category"] == "fact"
    assert row["epistemic_status"] == "proposed"


def test_console_coding_job_matches_the_database(client, role_urls, monkeypatch) -> None:
    assert CODING_PATH in CODE
    assert "终态是 FINISHED 或 FAILED" in CODE
    owner_id, project_id, csrf = _session(client, "console-record-code-project")
    task = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers=_headers(csrf, "console-record-code-task"),
        json={"goal": "改一处断言"},
    )
    assert task.status_code == 202, task.text
    workspace = {
        "kind": "aether.coding_workspace",
        "base_files": {"app.py": "def value():\n    return 0\n"},
        # The candidate does not pass yet: the test-only upstream's file_editor edit makes it return 42.
        "files": {"app.py": "def value():\n    return 0\n"},
        "tests": {"test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n"},
    }
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers=_headers(csrf, "console-record-code-file"),
        files={"file": ("base.json", json.dumps(workspace).encode(), "application/json")},
    )
    assert uploaded.status_code == 201, uploaded.text
    objective = "只改测试，不改可信评分器"
    job_key = "edit-assertion"
    created = client.post(
        f"/v1/projects/{project_id}/coding-jobs",
        headers=_headers(csrf, "console-record-code-job"),
        json={
            "task_id": task.json()["resource_id"],
            "base_artifact_id": uploaded.json()["artifact_id"],
            "job_key": job_key,
            "objective": objective,
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "accepted"
    assert objective not in created.text
    worker = Database(role_urls["aether_worker"])
    store = ArtifactStore(client.app.state.settings.artifact_root, worker)
    model = FakeCodingModel()
    with editing_upstream(model) as upstream_base:
        # In-process gateway (no gateway role): the job's model socket proxy relays to this upstream.
        for name in ("AETHER_GATEWAY_BASE", "AETHER_LOOPBACK_TOKEN", "AETHER_LIVE_MODEL_ALIAS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AETHER_LIVE_MODEL_BASE", upstream_base)
        monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", PROVIDER_KEY)
        route_pending(Database(role_urls["aether_dispatcher"]), worker, Database(role_urls["aether_verifier"]), store)
    assert model.problems == []
    assert model.authorizations and set(model.authorizations) == {f"Bearer {PROVIDER_KEY}"}
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT state, patch_artifact_id::text AS patch_id, result_artifact_id::text AS result_id "
                "FROM coding_jobs WHERE project_id = :project_id AND job_key = :job_key"
            ),
            {"project_id": project_id, "job_key": job_key},
        ).mappings().one()
        command = conn.execute(
            text("SELECT status FROM command_outbox WHERE project_id = :project_id AND kind = 'coding.create'"),
            {"project_id": project_id},
        ).scalar_one()
    assert row["state"] == "FINISHED"
    assert command == "DELIVERED"
    patch, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=row["patch_id"])
    result, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=row["result_id"])
    assert b"return 42" in patch
    assert b'"passed":1' in result or b'"passed": 1' in result
