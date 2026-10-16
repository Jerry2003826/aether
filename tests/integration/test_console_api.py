import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.console_commands import apply_console_commands
from aether_worker.service import route_pending
from tests.integration.test_auth import ORIGIN, _bootstrap

SHA = "b" * 64
HTML = "<img src=x onerror=alert(1)>"
LONG = "长文本" * 80


def _session(client, key: str):
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    headers = {**ORIGIN, "X-CSRF-Token": body["csrf_token"], "Idempotency-Key": key}
    project = client.post("/v1/projects", headers=headers, json={"name": "控制台"})
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"], body["csrf_token"]


def _headers(csrf: str, key: str, match: str | None = None) -> dict[str, str]:
    headers = {**ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key}
    if match is not None:
        headers["If-Match"] = match
    return headers


def test_sources_watches_and_ingestions_round_trip(client, role_urls) -> None:
    denied = client.get(f"/v1/projects/{uuid.uuid4()}/sources")
    assert denied.status_code == 401
    owner_id, project_id, csrf = _session(client, "console-source-project")
    created = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=_headers(csrf, "console-source-create"),
        json={"kind": "url", "locator": HTML + LONG, "allowed_domains": ["example.test"], "fetch_interval_seconds": 3600},
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "accepted"
    repeated = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=_headers(csrf, "console-source-create"),
        json={"kind": "url", "locator": HTML + LONG, "allowed_domains": ["example.test"], "fetch_interval_seconds": 3600},
    )
    assert repeated.status_code == 202
    assert repeated.json()["resource_id"] == created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    listed = client.get(f"/v1/projects/{project_id}/sources")
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["locator"] == HTML + LONG
    assert item["index_status"] == "not_indexed"
    assert HTML in listed.text
    ingested = client.post(
        f"/v1/projects/{project_id}/ingestions",
        headers=_headers(csrf, "console-ingest"),
        json={"source_id": item["id"]},
    )
    assert ingested.status_code == 202, ingested.text
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    jobs = client.get(f"/v1/projects/{project_id}/ingestions")
    assert jobs.json()["items"][0]["status"] == "QUEUED"
    assert jobs.json()["items"][0]["source_version_id"] is None
    watched = client.post(
        f"/v1/projects/{project_id}/watches",
        headers=_headers(csrf, "console-watch"),
        json={
            "purpose": "跟踪" + LONG,
            "source_ids": [item["id"]],
            "schedule": {"interval_seconds": 3600, "timezone": "Australia/Melbourne"},
            "budget": {"max_tokens": 1000},
        },
    )
    assert watched.status_code == 202, watched.text
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    watches = client.get(f"/v1/projects/{project_id}/watches")
    watch = watches.json()["items"][0]
    assert watch["status"] == "DRAFT"
    assert watch["purpose"].startswith("跟踪长文本")
    activated = client.patch(
        f"/v1/projects/{project_id}/watches/{watch['id']}",
        headers=_headers(csrf, "console-watch-active", "1"),
        json={"status": "ACTIVE"},
    )
    assert activated.status_code == 202, activated.text
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    again = client.get(f"/v1/projects/{project_id}/watches")
    assert again.json()["items"][0]["status"] == "DRAFT"


def test_recommendations_feedback_and_costs_read_the_ledger(client, role_urls) -> None:
    owner_id, project_id, csrf = _session(client, "console-reco-project")
    worker = Database(role_urls["aether_worker"])
    created = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=_headers(csrf, "console-reco-source"),
        json={"kind": "local_document", "locator": "笔记 " + HTML},
    )
    assert created.status_code == 202, created.text
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    recommended = client.get(f"/v1/projects/{project_id}/recommendations")
    assert recommended.status_code == 200, recommended.text
    body = recommended.json()
    assert body["impression_id"] is None
    assert body["items"][0]["title"] == "笔记 " + HTML
    assert body["items"][0]["exploration"] is False
    assert body["policy_version"] == "score-v1"
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        impressions = conn.execute(
            text("SELECT COUNT(*) FROM recommendation_impressions WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one()
    assert int(impressions) == 0
    event_id = str(uuid.uuid4())
    feedback = client.post(
        f"/v1/projects/{project_id}/feedback",
        headers=_headers(csrf, "console-feedback"),
        json={
            "event_id": event_id,
            "source_id": body["items"][0]["source_id"],
            "kind": "hide",
            "event_time": "2026-10-15T00:00:00Z",
        },
    )
    assert feedback.status_code == 202, feedback.text
    rejected = client.post(
        f"/v1/projects/{project_id}/feedback",
        headers=_headers(csrf, "console-feedback-user"),
        json={
            "event_id": str(uuid.uuid4()),
            "source_id": body["items"][0]["source_id"],
            "kind": "hide",
            "event_time": "2026-10-15T00:00:00Z",
            "user": owner_id,
        },
    )
    assert rejected.status_code == 422
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM feedback_events WHERE project_id = :project_id AND event_key = :event_key"),
            {"project_id": project_id, "event_key": event_id},
        ).scalar_one()
    assert int(count) == 1
    costs = client.get(f"/v1/projects/{project_id}/costs")
    assert costs.status_code == 200
    assert costs.json()["unknown_request_count"] == 0
    assert costs.json()["actual_known_micro_usd"] == 0
    missing = client.post(
        f"/v1/projects/{project_id}/search",
        headers=ORIGIN | {"X-CSRF-Token": csrf},
        json={"project_id": project_id, "query": "锁文件", "mode": "hybrid"},
    )
    assert missing.status_code == 503
    assert missing.json()["code"] == "index_unavailable"


def test_skill_import_does_not_score_or_execute(client, role_urls) -> None:
    owner_id, project_id, csrf = _session(client, "console-skill-project")
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers=_headers(csrf, "console-skill-file"),
        files={"file": ("skill.md", "技能包".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    artifact_id = uploaded.json()["artifact_id"]
    imported = client.post(
        f"/v1/projects/{project_id}/skills",
        headers=_headers(csrf, "console-skill-import"),
        json={"bundle_artifact_id": artifact_id},
    )
    assert imported.status_code == 202, imported.text
    worker = Database(role_urls["aether_worker"])
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    skills = client.get(f"/v1/projects/{project_id}/skills")
    skill = skills.json()["items"][0]
    assert skill["versions"][0]["status"] == "QUARANTINED"
    assert skill["description"] == "隔离导入，脚本未执行"
    scored = client.post(
        f"/v1/projects/{project_id}/skills/{skill['id']}/evaluate",
        headers=_headers(csrf, "console-skill-score"),
        json={
            "candidate_version_id": str(uuid.uuid4()),
            "dataset_manifest_id": artifact_id,
            "budget": {"max_tokens": 10},
            "score": 1,
        },
    )
    assert scored.status_code == 422
    version_id = None
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        version_id = conn.execute(
            text("SELECT id::text FROM skill_versions WHERE project_id = :project_id AND skill_id = :skill_id"),
            {"project_id": project_id, "skill_id": skill["id"]},
        ).scalar_one()
    queued = client.post(
        f"/v1/projects/{project_id}/skills/{skill['id']}/evaluate",
        headers=_headers(csrf, "console-skill-eval"),
        json={"candidate_version_id": version_id, "dataset_manifest_id": artifact_id, "budget": {"max_tokens": 10}},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] == "accepted"
    verifier = Database(role_urls["aether_verifier"])
    route_pending(
        Database(role_urls["aether_dispatcher"]),
        worker,
        verifier,
        ArtifactStore(client.app.state.settings.artifact_root, worker),
    )
    evaluations = client.get(f"/v1/projects/{project_id}/evaluations")
    assert evaluations.status_code == 200
    assert evaluations.json()["items"][0]["status"] == "REJECTED"
    assert evaluations.json()["items"][0]["metrics"] is None
    assert "Infinity" not in evaluations.text
    assert "NaN" not in evaluations.text
    row = {"id": str(uuid.uuid4())}
    api = Database(role_urls["aether_api"])
    try:
        with api.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO evaluation_runs (project_id, purpose, dataset_manifest_sha256, model_manifest, configuration, status, metrics) "
                    "VALUES (:project_id, 'development', :sha, '{}'::jsonb, '{}'::jsonb, 'ELIGIBLE', '{\"promoted\": true}'::jsonb)"
                ),
                {"project_id": project_id, "sha": SHA},
            )
        raised = False
    except DBAPIError:
        raised = True
    assert raised is True
    deployment = client.post(
        f"/v1/projects/{project_id}/skills/{skill['id']}/deployments",
        headers=_headers(csrf, "console-skill-deploy", "0"),
        json={
            "action": "promote",
            "target_version_id": version_id,
            "evaluation_id": row["id"],
            "expected_previous_version_id": None,
            "approval_id": str(uuid.uuid4()),
            "report_sha256": SHA,
        },
    )
    assert deployment.status_code == 202, deployment.text
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM skill_deployments WHERE project_id = :project_id AND skill_id = :skill_id"),
            {"project_id": project_id, "skill_id": skill["id"]},
        ).scalar_one()
        status = conn.execute(
            text("SELECT status FROM skill_versions WHERE id = :id"),
            {"id": version_id},
        ).scalar_one()
    assert int(count) == 0
    assert status == "QUARANTINED"


def test_connector_secret_is_not_readable_and_probes_do_not_pass(client, role_urls) -> None:
    owner_id, project_id, csrf = _session(client, "console-connector-project")
    secret = "sk-live-connector-secret"
    registered = client.post(
        "/v1/admin/connectors",
        headers=_headers(csrf, "console-connector"),
        json={
            "project_id": project_id,
            "connector_key": "local-echo",
            "transport": "stdio",
            "manifest": {"tools": ["echo"]},
            "secret_value": secret,
        },
    )
    assert registered.status_code == 202, registered.text
    assert secret not in registered.text
    worker = Database(role_urls["aether_worker"])
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    listed = client.get("/v1/admin/connectors")
    assert listed.status_code == 200, listed.text
    assert secret not in listed.text
    item = listed.json()["items"][0]
    assert item["secret_configured"] is True
    assert item["review_status"] == "reviewed"
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        stored = conn.execute(text("SELECT secret_reference, reviewed_manifest::text AS manifest FROM connector_configs WHERE id = :id"), {"id": item["id"]}).mappings().one()
        payload = conn.execute(text("SELECT payload::text FROM command_outbox WHERE project_id = :project_id AND kind = 'connector.register'"), {"project_id": project_id}).scalar_one()
    assert secret not in stored["secret_reference"]
    assert secret not in stored["manifest"]
    assert secret not in payload
    before = stored["manifest"]
    probe = client.post(f"/v1/admin/connectors/{item['id']}/probe", headers=_headers(csrf, "console-connector-probe"))
    assert probe.status_code == 200, probe.text
    assert probe.json()["status"] == "unavailable"
    assert probe.json()["checks"][1]["check"] == "scope_unchanged"
    model = client.post(
        "/v1/admin/models/probe",
        headers=_headers(csrf, "console-model-probe"),
        json={"alias": "scripted", "allow_billable_probe": True, "max_cost_micro_usd": 1},
    )
    assert model.status_code == 200, model.text
    assert model.json()["status"] == "unavailable"
    assert model.json()["checks"][0]["passed"] is False
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        after = conn.execute(text("SELECT reviewed_manifest::text FROM connector_configs WHERE id = :id"), {"id": item["id"]}).scalar_one()
    assert after == before


def test_expired_approval_stays_out_of_the_pending_list(client, role_urls) -> None:
    owner_id, project_id, _csrf = _session(client, "console-approval-project")
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_id = conn.execute(
            text("INSERT INTO tasks (project_id, original_goal, workflow_id) VALUES (:project_id, '过期审批', :workflow) RETURNING id::text"),
            {"project_id": project_id, "workflow": f"expired-{project_id}"},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, decision, owner_id, expires_at) "
                "VALUES (:project_id, :task_id, :digest, '{}'::jsonb, 'pending', :owner, :expires)"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "digest": SHA,
                "owner": owner_id,
                "expires": datetime.now(UTC) - timedelta(minutes=5),
            },
        )
    pending = client.get(f"/v1/projects/{project_id}/approvals")
    assert pending.status_code == 200
    assert pending.json()["items"] == []
