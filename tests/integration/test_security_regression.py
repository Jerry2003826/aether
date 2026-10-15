import asyncio
import json
import ipaddress
import threading
from datetime import UTC, datetime, timedelta

from psycopg.errors import InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aether_storage.artifacts import ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_storage.operations import OperationError, OperationLedger
from aether_worker.console_commands import apply_console_commands
from aether_worker.dispatcher import deliver_pending
from aether_worker.ingest import IngestError, assert_public_target
from aether_worker.memory import apply_memory_commands
from aether_worker.outbox import enqueue_task
from aether_worker.retention import commit_claim_text, current_epoch
from aether_worker.runner import LinuxRunner, docker_available, release_containers
from aether_worker.security_regression import release_decision, run_carriers
from aether_worker.verification import export_candidate, grade, text_sha256
from aether_worker.workflows import TaskWorkflow
from reference.models import ArtifactRef, CodingJobRequest
from tests.integration.conftest import insert_task
from tests.integration.test_auth import ORIGIN, _bootstrap
from tests.integration.test_runner import PROVIDER_KEY

SHA = "c" * 64


class _Down:
    async def start_workflow(self, *args, **kwargs):
        raise ConnectionError("temporal unavailable")

    def get_workflow_handle(self, workflow_id: str):
        raise ConnectionError("temporal unavailable")


def _owner(client, key: str):
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    project = client.post(
        "/v1/projects",
        headers={**ORIGIN, "X-CSRF-Token": body["csrf_token"], "Idempotency-Key": key},
        json={"name": key},
    )
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"], body["csrf_token"]


def test_probes_block_release_even_when_the_classifier_score_is_high(client, role_urls, tmp_path) -> None:
    if not docker_available():
        raise AssertionError("network probe needs the container runner")
    owner_id, project_id, csrf = _owner(client, "security-probes")
    worker = Database(role_urls["aether_worker"])
    api = Database(role_urls["aether_api"])
    try:
        _security_probes(client, role_urls, tmp_path, owner_id, project_id, csrf, worker, api)
    finally:
        worker.engine.dispose()
        api.engine.dispose()


def _security_probes(client, role_urls, tmp_path, owner_id, project_id, csrf, worker, api) -> None:
    probes: list[dict] = []

    def ssrf(url: str, addresses: list[str]) -> None:
        try:
            assert_public_target(url, resolve=lambda _host: [ipaddress.ip_address(item) for item in addresses])
        except IngestError as exc:
            probes.append({"name": url, "blocked": exc.code == "ssrf_rejected", "evidence": exc.code})
            return
        probes.append({"name": url, "blocked": False, "evidence": "connected"})

    ssrf("http://127.0.0.1/secret", ["127.0.0.1"])
    ssrf("http://169.254.169.254/latest", ["169.254.169.254"])
    ssrf("http://rebind.example/path", ["1.1.1.1", "10.0.0.8"])
    try:
        assert_public_target("https://example.com/", resolve=lambda _host: [ipaddress.ip_address("1.1.1.1")], allow_hosts={"example.com"})
        probes.append({"name": "allow_hosts", "blocked": False, "evidence": "fixture_switch_open"})
    except IngestError as exc:
        probes.append({"name": "allow_hosts", "blocked": exc.code == "fixture_hosts_rejected", "evidence": exc.code})

    try:
        with api.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO evaluation_runs (project_id, purpose, dataset_manifest_sha256, model_manifest, configuration, status) "
                    "VALUES (:project_id, 'development', :sha, '{}'::jsonb, '{}'::jsonb, 'ELIGIBLE')"
                ),
                {"project_id": project_id, "sha": SHA},
            )
        probes.append({"name": "identity", "blocked": False, "evidence": "api_wrote_score"})
    except DBAPIError as exc:
        probes.append({"name": "identity", "blocked": isinstance(exc.orig, InsufficientPrivilege), "evidence": type(exc.orig).__name__})

    task_id = insert_task(worker, owner_id, project_id, "审批探针")
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        approval_id = conn.execute(
            text(
                "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, decision, owner_id, expires_at) "
                "VALUES (:project_id, :task_id, :digest, '{}'::jsonb, 'pending', :owner, :expires) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "digest": SHA, "owner": owner_id, "expires": datetime.now(UTC) + timedelta(hours=1)},
        ).scalar_one()
    ledger = OperationLedger(worker)
    try:
        ledger.decide(owner_id=owner_id, project_id=project_id, task_id=task_id, approval_id=str(approval_id), decision="approved", expected_intent_hash="d" * 64)
        probes.append({"name": "approval", "blocked": False, "evidence": "wrong_hash_accepted"})
    except OperationError as exc:
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            decision = conn.execute(text("SELECT decision FROM approvals WHERE id = :id"), {"id": approval_id}).scalar_one()
        probes.append({"name": "approval", "blocked": exc.code == "approval_rejected" and decision == "pending", "evidence": decision})

    budget = BudgetLedger(worker)
    budget.open_account(owner_id=owner_id, project_id=project_id, task_id=task_id, limit_micro_usd=100, token_limit=10, max_requests=4, verification_reserve_fraction=0)
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt(key: str) -> None:
        barrier.wait()
        results.append(budget.reserve(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=key, amount=60, role="actor"))

    threads = [threading.Thread(target=attempt, args=("race-a",)), threading.Thread(target=attempt, args=("race-b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    held = budget.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id)["held_micro_usd"]
    probes.append({"name": "budget_race", "blocked": sorted(results) == [False, True] and held == 60, "evidence": {"results": results, "held": held}})

    created = client.post(
        f"/v1/projects/{project_id}/skills",
        headers={**ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "security-skill"},
        json={"bundle_artifact_id": _artifact(client, project_id, csrf)},
    )
    assert created.status_code == 202, created.text
    apply_console_commands(worker, owner_id=owner_id, project_id=project_id)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        status = conn.execute(text("SELECT status FROM skill_versions WHERE project_id = :project_id"), {"project_id": project_id}).scalar_one()
    probes.append({"name": "import", "blocked": status == "QUARANTINED", "evidence": status})

    memory = client.post(
        f"/v1/projects/{project_id}/memories",
        headers={**ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "security-memory"},
        json={"claim": "原来的说法", "kind": "semantic", "category": "fact"},
    )
    assert memory.status_code == 202, memory.text
    apply_memory_commands(worker, ArtifactStore(tmp_path / "artifacts", worker), owner_id=owner_id, project_id=project_id)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        seen = current_epoch(conn, project_id)
        conn.execute(text("UPDATE projects SET visibility_epoch = visibility_epoch + 1 WHERE id = :id"), {"id": project_id})
        wrote = commit_claim_text(conn, project_id=project_id, memory_id=memory.json()["resource_id"], claim="写回去的说法", seen_epoch=seen)
        claim = conn.execute(text("SELECT claim FROM memories WHERE id = :id"), {"id": memory.json()["resource_id"]}).scalar_one()
    probes.append({"name": "delete_rewrite", "blocked": wrote is False and claim == "原来的说法", "evidence": claim})

    carriers = run_carriers(tmp_path / "carriers")
    decision = release_decision(carriers, probes, classifier_score=0.99)
    assert decision["allowed"] is True
    assert all(item["blocked"] for item in probes)
    forced = release_decision(carriers, [*probes, {"name": "planted", "blocked": False}], classifier_score=0.99)
    assert forced["allowed"] is False
    assert forced["open_probes"] == ["planted"]


def test_fault_matrix_checks_effects(client, api_db, role_urls, tmp_path) -> None:
    if not docker_available():
        raise AssertionError("runner restart probe needs docker")
    owner_id, project_id, _csrf = _owner(client, "security-faults")
    worker = Database(role_urls["aether_worker"])
    dispatcher = Database(role_urls["aether_dispatcher"])
    try:
        _fault_matrix(client, api_db, role_urls, tmp_path, owner_id, project_id, _csrf, worker, dispatcher)
    finally:
        worker.engine.dispose()
        dispatcher.engine.dispose()


def _fault_matrix(client, api_db, role_urls, tmp_path, owner_id, project_id, _csrf, worker, dispatcher) -> None:
    created = enqueue_task(api_db, owner_id=owner_id, project_id=project_id, goal="未投递", command_key="fault-pending")
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        task_status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": created["task_id"]}).scalar_one()
        command_status = conn.execute(text("SELECT status FROM command_outbox WHERE command_key = 'fault-pending'")).scalar_one()
    queued = {"name": "db_without_temporal", "blocked": task_status == "QUEUED" and command_status == "PENDING", "evidence": {"task": task_status, "command": command_status}}

    workflow = TaskWorkflow()
    workflow.command({"command_id": "same"})
    workflow.command({"command_id": "same"})
    deduped = {"name": "ack_lost", "blocked": workflow.applied() == ["same"], "evidence": workflow.applied()}

    down = enqueue_task(api_db, owner_id=owner_id, project_id=project_id, goal="投递失败", command_key="fault-down")
    dispatcher = Database(role_urls["aether_dispatcher"])
    failed = asyncio.run(deliver_pending(dispatcher, _Down(), task_queue="aether-fault"))  # type: ignore[arg-type]
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        down_task = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": down["task_id"]}).scalar_one()
        down_command = conn.execute(text("SELECT status FROM command_outbox WHERE command_key = 'fault-down'")).scalar_one()
    temporal = {
        "name": "temporal_reject",
        "blocked": "fault-down" in failed["failed"] and "fault-down" not in failed["delivered"] and down_task == "QUEUED" and down_command == "DELIVERING",
        "evidence": {"failed": failed["failed"], "task": down_task, "command": down_command},
    }

    budget_task = insert_task(worker, owner_id, project_id, "费用")
    budget = BudgetLedger(worker)
    budget.open_account(owner_id=owner_id, project_id=project_id, task_id=budget_task, limit_micro_usd=100, token_limit=10, max_requests=4, verification_reserve_fraction=0)
    assert budget.reserve(owner_id=owner_id, project_id=project_id, task_id=budget_task, request_key="once", amount=30, role="actor") is True
    assert budget.reserve(owner_id=owner_id, project_id=project_id, task_id=budget_task, request_key="once", amount=30, role="actor") is True
    held_once = budget.snapshot(owner_id=owner_id, project_id=project_id, task_id=budget_task)["held_micro_usd"] == 30
    budget.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=budget_task, request_key="once")
    snap = budget.snapshot(owner_id=owner_id, project_id=project_id, task_id=budget_task)
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        actual = conn.execute(text("SELECT actual_micro_usd FROM model_requests WHERE task_id = :id"), {"id": budget_task}).scalar_one()
    ledger = {
        "name": "timeout_holds_budget",
        "blocked": held_once and snap["held_micro_usd"] == 30 and snap["unknown_request_count"] == 1 and actual is None,
        "evidence": {"held": snap["held_micro_usd"], "unknown": snap["unknown_request_count"], "actual": actual},
    }

    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    candidate.mkdir()
    suite.mkdir()
    (candidate / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (suite / "test_app.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    graded = grade(
        candidate,
        suite,
        suite_sha256=export_candidate(suite)["candidate_sha256"],
        contract_text="合同",
        contract_sha256=text_sha256("合同"),
        candidate_sha256="0" * 64,
    )
    stale = {"name": "stale_report", "blocked": graded["verdict"] == "FAIL", "evidence": graded["reason"]}

    request = CodingJobRequest(
        project_id="project-fault",
        task_id="task-fault",
        intent_id="intent-fault",
        base_artifact=ArtifactRef(artifact_id="artifact-base", project_id="project-fault", sha256="a" * 64, byte_size=1, media_type="text/plain"),
        objective="修改说明",
        contract_sha256="b" * 64,
        allowed_tools=["tool.read"],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    runner = LinuxRunner(tmp_path / "runner")
    key = f"{request.project_id}:{request.task_id}:{request.intent_id}"
    try:
        first = runner.start(request, token="job-token-only", gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        second = runner.start(request, token="job-token-only", gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        saved = (tmp_path / "runner" / "jobs.json").read_text(encoding="utf-8")
        restarted = {"name": "runner_restart", "blocked": first.job_id == second.job_id and PROVIDER_KEY not in saved, "evidence": first.job_id}
        cancelled = runner.cancel(request.project_id, first.job_id)
        stored = json.loads((tmp_path / "runner" / "jobs.json").read_text(encoding="utf-8"))[key]
        dead = {"name": "runner_stopped", "blocked": cancelled.status == "CANCELLED" and stored["external_effect"] == "unknown", "evidence": stored["external_effect"]}
    finally:
        release_containers(key)

    replay_task = enqueue_task(api_db, owner_id=owner_id, project_id=project_id, goal="已投递", command_key="fault-delivered")
    visible = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**ORIGIN, "X-CSRF-Token": _csrf, "Idempotency-Key": "fault-event"},
        json={"goal": "要能重放"},
    )
    assert visible.status_code == 202, visible.text
    first_events = client.get(f"/v1/projects/{project_id}/tasks/{visible.json()['resource_id']}/events")
    event_id = next(line.removeprefix("id: ") for line in first_events.text.splitlines() if line.startswith("id: "))
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(text("UPDATE command_outbox SET status = 'DELIVERED' WHERE command_key = 'fault-delivered'"))
    again = asyncio.run(deliver_pending(dispatcher, _Down(), task_queue="aether-fault-2"))  # type: ignore[arg-type]
    with api_db.transaction(owner_id=owner_id, project_id=project_id) as conn:
        delivered_status = conn.execute(text("SELECT status FROM command_outbox WHERE command_key = 'fault-delivered'")).scalar_one()
        task_after = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": replay_task["task_id"]}).scalar_one()
    restored = {
        "name": "restore_does_not_resend",
        "blocked": "fault-delivered" not in again["delivered"] and "fault-delivered" not in again["failed"] and delivered_status == "DELIVERED" and task_after == "QUEUED",
        "evidence": {"command": delivered_status, "task": task_after},
    }
    streamed = client.get(f"/v1/projects/{project_id}/tasks/{visible.json()['resource_id']}/events", headers={"Last-Event-ID": event_id})
    bad_cursor = client.get(f"/v1/projects/{project_id}/tasks/{visible.json()['resource_id']}/events", headers={"Last-Event-ID": "not-a-real-event"})
    sse = {
        "name": "sse_cursor",
        "blocked": streamed.status_code == 200 and "event: task.event" not in streamed.text and bad_cursor.status_code == 422 and bad_cursor.json()["code"] == "cursor_expired",
        "evidence": {"replay": streamed.status_code, "expired": bad_cursor.status_code},
    }
    fresh = TaskWorkflow()
    fresh.command({"command_id": "replayed"})
    history = {"name": "workflow_history_is_per_instance", "blocked": fresh.applied() == ["replayed"] and workflow.applied() == ["same"], "evidence": fresh.applied()}

    approval_task = insert_task(worker, owner_id, project_id, "哈希")
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        approval_id = conn.execute(
            text(
                "INSERT INTO approvals (project_id, task_id, intent_digest, requested_payload, decision, owner_id, expires_at) "
                "VALUES (:project_id, :task_id, :digest, '{}'::jsonb, 'pending', :owner, now() + interval '1 hour') RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": approval_task, "digest": SHA, "owner": owner_id},
        ).scalar_one()
    try:
        OperationLedger(worker).decide(
            owner_id=owner_id,
            project_id=project_id,
            task_id=approval_task,
            approval_id=str(approval_id),
            decision="approved",
            expected_intent_hash="e" * 64,
        )
        approval_blocked = False
    except OperationError:
        approval_blocked = True
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        approval_decision = conn.execute(text("SELECT decision FROM approvals WHERE id = :id"), {"id": approval_id}).scalar_one()
    matrix = [
        queued,
        deduped,
        temporal,
        ledger,
        stale,
        restarted,
        dead,
        restored,
        sse,
        history,
        {"name": "lost_response_single_hold", "blocked": held_once, "evidence": snap["held_micro_usd"]},
        {"name": "approval_bound_to_hash", "blocked": approval_blocked and approval_decision == "pending", "evidence": approval_decision},
    ]
    assert len(matrix) == 12
    assert [item for item in matrix if not item["blocked"]] == []
    decision = release_decision(run_carriers(tmp_path / "carriers"), [{"name": item["name"], "blocked": item["blocked"]} for item in matrix], classifier_score=1)
    assert decision["allowed"] is True


def _artifact(client, project_id: str, csrf: str) -> str:
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "security-artifact"},
        files={"file": ("skill.md", "技能".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["artifact_id"]
