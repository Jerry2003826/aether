"""Turn an accepted task start into a durable artifact and an independent verification.

The activity is idempotent. A worker restart or a Temporal retry must not add a second
artifact, contract, or verification row for the same observation. Historical evidence
stays in place; a new observation gets a new row.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from temporalio import activity

from aether_domain.security import sha256_text
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_storage.operations import OperationLedger
from aether_worker.role_client import complete_via_gateway, verify_via_role
from aether_worker.research import (
    cites_fact,
    estimate_provider_request_tokens,
    goal_completion_detail,
    goal_completion_state,
    record_verification,
    required_facts,
    research_prompt,
    scripted_answer,
)

CRITERION_ID = "goal_artifact"
VERIFIER_VERSION = "goal-artifact-9"
_CRITERION_TEMPLATES = frozenset({
    "artifact_exists",
    "json_schema",
    "trusted_test_suite",
    "citation_integrity",
    "source_coverage",
    "human_review",
})
OUTPUT_TOKEN_CAP = 256
TRUNCATED_ANSWER = "模型输出因长度上限被截断，不能当作完整回答"
# Local dispatch ceiling, not a provider price list. A model priced above this
# bound can still report an overrun after the bill arrives.
INPUT_MICRO_USD_PER_TOKEN = 100
OUTPUT_MICRO_USD_PER_TOKEN = 400
DEFAULT_BUDGET = {
    "max_cost_micro_usd": 2_000_000,
    "max_tokens": 100_000,
    "max_model_requests": 48,
    "active_seconds": 1800,
    "verification_reserve_fraction": 0.2,
}
TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")
PAUSEABLE_STATUSES = ("QUEUED", "RUNNING", "VERIFYING", "WAITING_APPROVAL")
NON_TERMINAL_STATUSES = ("DRAFT", "QUEUED", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED", "NEEDS_REVIEW")
STOP_STATUSES = ("SUCCEEDED", "FAILED", "NEEDS_REVIEW", "CANCELLED", "PAUSED")
ACTIVE_STATUSES = {"RUNNING", "VERIFYING"}
REVIEW_TTL_SECONDS = 72 * 60 * 60


class ContractUnapproved(RuntimeError):
    """The worker must not invent the owner's approval."""


@dataclass(frozen=True)
class WorkerEndpoints:
    worker_url: str
    verifier_url: str
    dispatcher_url: str
    artifact_root: Path
    temporal_address: str
    task_queue: str
    intake_pause_seconds: float = 0
    research_provider: str = "live"
    live_model_base: str = ""
    live_model_alias: str = ""
    live_model_key: str = ""
    live_timeout_seconds: float = 30
    verifier_base: str = ""
    gateway_base: str = ""
    runner_base: str = ""
    loopback_token: str = ""


ENDPOINTS: WorkerEndpoints | None = None


def configure(endpoints: WorkerEndpoints) -> None:
    global ENDPOINTS
    ENDPOINTS = endpoints


def goal_document(task_id: str, goal: str) -> bytes:
    return json.dumps({"goal": goal, "task_id": task_id}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def canonical_json(value: object) -> str:
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


_CONTROL_EXECUTOR: concurrent.futures.Executor | None = None


def set_control_executor(executor: concurrent.futures.Executor | None) -> None:
    """Pause and cancel run here, not on the pool that blocks inside model calls."""
    global _CONTROL_EXECUTOR
    _CONTROL_EXECUTOR = executor


async def _await_off_loop(work: asyncio.Future, heartbeat: str):
    while True:
        done, _pending = await asyncio.wait({work}, timeout=2)
        if work in done:
            return work.result()
        try:
            activity.heartbeat(heartbeat)
        except RuntimeError:
            continue


async def _run_blocking(func, *args):
    """Run sync database, disk, and provider work off the activity event loop.

    The thread is not killed when the activity is cancelled. Publishing still
    checks pause and cancel after the thread returns, so a late response is a
    receipt rather than a new success.
    """
    work = asyncio.create_task(asyncio.to_thread(func, *args))
    return await _await_off_loop(work, "running")


async def run_control_activity(func, *args):
    """Run a control effect on the reserved executor when the worker has one."""
    executor = _CONTROL_EXECUTOR
    if executor is None:
        return await _run_blocking(func, *args)
    loop = asyncio.get_running_loop()
    work = loop.run_in_executor(executor, functools.partial(func, *args))
    return await _await_off_loop(work, "control")


@activity.defn
async def prepare_task(payload: dict) -> dict[str, str]:
    if ENDPOINTS is None:
        raise RuntimeError("worker endpoints are not configured")
    return await _run_blocking(prepare_task_effect, ENDPOINTS, payload)


@activity.defn
async def execute_task(payload: dict) -> dict[str, str]:
    if ENDPOINTS is None:
        raise RuntimeError("worker endpoints are not configured")
    try:
        return await _run_blocking(execute_task_effect, ENDPOINTS, payload)
    except ArtifactError:
        return await _run_blocking(_review_artifact_error, ENDPOINTS, payload)


@activity.defn
async def apply_task_control(payload: dict) -> dict[str, Any]:
    if ENDPOINTS is None:
        raise RuntimeError("worker endpoints are not configured")
    return await run_control_activity(apply_task_control_effect, ENDPOINTS, payload)


@activity.defn
async def materialize_goal(payload: dict) -> dict[str, str]:
    prepared = await prepare_task(payload)
    if prepared.get("status") != "prepared":
        return prepared
    return await execute_task(payload)


def prepare_task_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    project_id = str(payload.get("project_id") or "")
    task_id = str(payload.get("task_id") or "")
    command_id = str(payload.get("command_id") or "")
    if not project_id or not task_id or not command_id:
        return {"status": "ignored"}
    worker = Database(endpoints.worker_url)
    command = _command(worker, project_id, command_id)
    if command is None:
        return {"status": "ignored"}
    kind = str(command["kind"])
    if kind == "resolve_review":
        return _resolve_review(worker, command, project_id, task_id)
    if kind not in {"start", "approve_goal"}:
        return {"status": "ignored"}
    owner_id, goal = _owner_and_goal(worker, project_id, task_id)
    if not owner_id or not goal:
        raise RuntimeError("start command is missing owner_id or goal")
    blocked = _block_execution(worker, owner_id, project_id, task_id)
    if blocked is not None:
        return {"status": blocked, "task_id": task_id}
    gate = _open_goal_gate(
        worker,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        goal=goal,
        approval=_command_body(command) if kind == "approve_goal" else None,
        command_id=command_id,
    )
    if gate.get("status") != "prepared":
        return gate
    store = ArtifactStore(endpoints.artifact_root, worker)
    _ensure_artifact(
        worker,
        store,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        classification="task_goal",
        data=goal_document(task_id, goal),
        media_type="text/plain",
    )
    _event(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, event_key=f"running-{task_id}", event_type="task.running", payload={"status": "RUNNING"})
    return {"status": "prepared", "task_id": task_id}


def execute_task_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    command = _execution_context(endpoints, payload)
    if command is None:
        return {"status": "ignored"}
    owner_id, project_id, task_id, goal = command
    worker = Database(endpoints.worker_url)
    blocked = _block_execution(worker, owner_id, project_id, task_id)
    if blocked is not None:
        return {"status": blocked, "task_id": task_id}
    store = ArtifactStore(endpoints.artifact_root, worker)
    if endpoints.research_provider != "scripted":
        missing = _missing_live_settings(endpoints)
        if missing:
            return _needs_review(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="没有配置可用的模型供应商", code="live_provider_unconfigured", missing=missing)
    start_payload = _task_start_payload(worker, project_id, task_id)
    allowed_ids = start_payload.get("allowed_source_ids")
    allowed = allowed_ids if isinstance(allowed_ids, list) else None
    sources, manifest, _snapshot, source_state, missing_sources = _read_sources(worker, store, owner_id, project_id, allowed_ids=allowed)
    if source_state == "empty":
        return _needs_review(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="允许的来源是空的", code="allowed_sources_empty")
    if source_state == "invalid":
        return _needs_review(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="允许的来源不合法或不可读", code="allowed_sources_invalid", missing=missing_sources)
    if not sources:
        return _needs_review(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="没有可读的来源", code="sources_missing")
    if _active_time_exhausted(worker, owner_id, project_id, task_id):
        return _mark_budget_exhausted(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, limit="active_seconds")
    blocked = _block_execution(worker, owner_id, project_id, task_id)
    if blocked is not None:
        return {"status": blocked, "task_id": task_id}
    if endpoints.research_provider == "scripted":
        answer = _ensure_answer(worker, store, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal, sources=sources, manifest=manifest)
    else:
        answer = _ensure_live_answer(
            endpoints,
            worker,
            store,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            goal=goal,
            budget=start_payload.get("budget"),
            allowed_ids=allowed,
        )
        if isinstance(answer, dict):
            return answer
    if answer is None:
        return {"status": _task_status(worker, owner_id, project_id, task_id), "task_id": task_id}
    if not answer:
        return _needs_review(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="没有形成答案", code="answer_missing")
    blocked = _block_execution(worker, owner_id, project_id, task_id)
    if blocked is not None:
        return {"status": blocked, "task_id": task_id, "artifact_id": answer}
    _set_status(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, status="VERIFYING", allowed=("RUNNING", "VERIFYING"))
    try:
        contract_id, contract_sha = _ensure_contract(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal)
    except ContractUnapproved:
        return _needs_review(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="合同还没有所有者批准", code="contract_unapproved")
    verification_id = _ensure_verification(
        endpoints,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        contract_id=contract_id,
        contract_sha=contract_sha,
        artifact_id=answer,
        goal=goal,
        allowed_ids=allowed,
    )
    verdict = _verdict(endpoints, owner_id, project_id, verification_id)
    if verdict == "PASS":
        terminal = "SUCCEEDED"
    elif verdict == "UNVERIFIABLE":
        terminal = "NEEDS_REVIEW"
    else:
        terminal = "FAILED"
    actual = _commit_terminal(worker, owner_id=owner_id, project_id=project_id, task_id=task_id, status=terminal)
    return {"status": actual, "task_id": task_id, "verification_id": verification_id, "artifact_id": answer}


def apply_task_control_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, Any]:
    project_id = str(payload.get("project_id") or "")
    task_id = str(payload.get("task_id") or "")
    command_id = str(payload.get("command_id") or "")
    if not project_id or not task_id or not command_id:
        return {"status": "ignored", "resumed": False}
    worker = Database(endpoints.worker_url)
    command = _command(worker, project_id, command_id)
    if command is None or str(command["kind"]) not in {"pause", "resume", "cancel"}:
        return {"status": "ignored", "resumed": False}
    owner_id, _goal = _owner_and_goal(worker, project_id, task_id)
    state, resume = _control_state(worker, owner_id, project_id, task_id)
    kind = str(command["kind"])
    if kind == "pause":
        if state in TERMINAL_STATUSES:
            return {"status": state, "task_id": task_id, "resumed": False}
        if state == "PAUSED":
            return {"status": "PAUSED", "task_id": task_id, "resumed": False}
        actual = _set_status(
            worker,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            status="PAUSED",
            allowed=PAUSEABLE_STATUSES,
            resume_state="pause_requested",
        )
        BudgetLedger(worker).revoke_unsent(owner_id=owner_id, project_id=project_id, task_id=task_id)
        return {"status": actual, "task_id": task_id, "resumed": False}
    if kind == "resume":
        if resume == "cancel_requested" or state == "CANCELLED":
            if state in TERMINAL_STATUSES:
                return {"status": state, "task_id": task_id, "resumed": False}
            actual = _set_status(
                worker,
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                status="CANCELLED",
                allowed=NON_TERMINAL_STATUSES,
                resume_state="cancel_requested",
            )
            BudgetLedger(worker).revoke_unsent(owner_id=owner_id, project_id=project_id, task_id=task_id)
            return {"status": actual, "task_id": task_id, "resumed": False}
        if state != "PAUSED":
            return {"status": state, "task_id": task_id, "resumed": False}
        actual = _set_status(
            worker,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            status="RUNNING",
            allowed=("PAUSED",),
            clear_resume=True,
        )
        return {"status": actual, "task_id": task_id, "resumed": actual == "RUNNING"}
    if state in TERMINAL_STATUSES:
        return {"status": state, "task_id": task_id, "resumed": False}
    actual = _set_status(
        worker,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        status="CANCELLED",
        allowed=NON_TERMINAL_STATUSES,
        resume_state="cancel_requested",
    )
    BudgetLedger(worker).revoke_unsent(owner_id=owner_id, project_id=project_id, task_id=task_id)
    return {"status": actual, "task_id": task_id, "resumed": False}


def _start_command(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> tuple[str, str, str, str] | None:
    project_id = str(payload.get("project_id") or "")
    task_id = str(payload.get("task_id") or "")
    command_id = str(payload.get("command_id") or "")
    if not project_id or not task_id or not command_id:
        return None
    worker = Database(endpoints.worker_url)
    command = _command(worker, project_id, command_id)
    if command is None or str(command["kind"]) != "start":
        return None
    body = command["payload"] if isinstance(command["payload"], dict) else json.loads(command["payload"])
    owner_id = str(body.get("owner_id") or "")
    goal = str(body.get("goal") or "")
    if not owner_id or not goal:
        raise RuntimeError("start command is missing owner_id or goal")
    return owner_id, project_id, task_id, goal


def _execution_context(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> tuple[str, str, str, str] | None:
    started = _start_command(endpoints, payload)
    if started is not None:
        return started
    project_id = str(payload.get("project_id") or "")
    task_id = str(payload.get("task_id") or "")
    command_id = str(payload.get("command_id") or "")
    if not project_id or not task_id or not command_id:
        return None
    worker = Database(endpoints.worker_url)
    command = _command(worker, project_id, command_id)
    if command is None or str(command["kind"]) not in {"resume", "approve_goal", "resolve_review"}:
        return None
    if str(command["kind"]) == "resolve_review" and str(_command_body(command).get("decision") or "") != "approve":
        return None
    owner_id, goal = _owner_and_goal(worker, project_id, task_id)
    if not owner_id or not goal:
        return None
    return owner_id, project_id, task_id, goal


def materialize_goal_effect(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    prepared = prepare_task_effect(endpoints, payload)
    if prepared.get("status") != "prepared":
        return prepared
    return execute_task_effect(endpoints, payload)


def _command(database: Database, project_id: str, command_id: str) -> dict[str, Any] | None:
    with database.transaction(project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT kind, payload FROM command_outbox WHERE project_id = :project_id AND command_key = :command_id"),
            {"project_id": project_id, "command_id": command_id},
        ).mappings().first()
    return None if row is None else dict(row)


def _owner_and_goal(database: Database, project_id: str, task_id: str) -> tuple[str, str]:
    with database.transaction(project_id=project_id) as conn:
        goal = conn.execute(
            text("SELECT original_goal FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).scalar_one()
        payload = conn.execute(
            text(
                "SELECT payload FROM command_outbox WHERE project_id = :project_id AND task_id = :id AND kind = 'start' "
                "ORDER BY created_at LIMIT 1"
            ),
            {"project_id": project_id, "id": task_id},
        ).scalar()
    body = payload if isinstance(payload, dict) else json.loads(payload) if payload else {}
    return str(body.get("owner_id") or ""), str(goal)


def _ensure_contract(database: Database, *, owner_id: str, project_id: str, task_id: str, goal: str, approved_by: str | None = None) -> tuple[str, str]:
    existing = _load_authoritative_contract(database, owner_id, project_id, task_id)
    if existing is not None:
        with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "UPDATE tasks SET current_contract_id = :contract_id "
                    "WHERE project_id = :project_id AND id = :task_id AND current_contract_id IS NULL"
                ),
                {"contract_id": existing["id"], "project_id": project_id, "task_id": task_id},
            )
        return existing["id"], existing["digest"]
    if not approved_by:
        raise ContractUnapproved("contract is not approved")
    content = _frozen_contract(database, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal)
    raw = canonical_json(content)
    digest = sha256_text(raw)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        contract_id = conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project_id, :task_id, 1, CAST(:content AS jsonb), :digest, :owner, now()) RETURNING id::text"
            ),
            {"project_id": project_id, "task_id": task_id, "content": raw, "digest": digest, "owner": approved_by},
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE tasks SET current_contract_id = :contract_id "
                "WHERE project_id = :project_id AND id = :task_id AND current_contract_id IS NULL"
            ),
            {"contract_id": contract_id, "project_id": project_id, "task_id": task_id},
        )
    return str(contract_id), digest


def _command_body(command: dict[str, Any]) -> dict[str, Any]:
    payload = command.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        loaded = json.loads(payload)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _is_project_owner(database: Database, owner_id: str, project_id: str) -> bool:
    if not owner_id:
        return False
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT 1 FROM projects WHERE id = :id AND owner_id = :owner AND deleted_at IS NULL"),
            {"id": project_id, "owner": owner_id},
        ).first()
    return found is not None


def _publish_draft(database: Database, *, owner_id: str, project_id: str, task_id: str, goal: str) -> str:
    frozen = _frozen_contract(database, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal)
    digest = sha256_text(canonical_json(frozen))
    existing = _load_draft(database, owner_id, project_id, task_id)
    if existing is None:
        published: dict[str, Any] = {
            "goal": str(frozen.get("goal") or goal),
            "contract_sha256": digest,
            "contract_version": 1,
            "project_id": project_id,
            "task_id": task_id,
        }
        if "criterion_id" in frozen:
            published["criterion_id"] = frozen["criterion_id"]
        if "criteria" in frozen:
            published["criteria"] = frozen["criteria"]
        if "required_artifacts" in frozen:
            published["required_artifacts"] = frozen["required_artifacts"]
    else:
        published = dict(existing)
        published["contract_sha256"] = digest
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE tasks SET draft_contract = CAST(:draft AS jsonb), version = version + 1 WHERE project_id = :project_id AND id = :id"),
            {"draft": json.dumps(published), "project_id": project_id, "id": task_id},
        )
    return digest


def _matching_approval(database: Database, *, project_id: str, task_id: str, digest: str) -> dict[str, Any] | None:
    with database.transaction(project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT payload FROM command_outbox WHERE project_id = :project_id AND task_id = :task_id AND kind = 'approve_goal' "
                "ORDER BY created_at"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).scalars().all()
    for payload in rows:
        body = payload if isinstance(payload, dict) else json.loads(payload) if isinstance(payload, str) else {}
        if not isinstance(body, dict):
            continue
        owner_id = str(body.get("owner_id") or "")
        expected = str(body.get("expected_contract_sha256") or "")
        if expected and expected != digest:
            continue
        if _is_project_owner(database, owner_id, project_id):
            return body
    return None


def _command_was_prepared(database: Database, owner_id: str, project_id: str, task_id: str, command_id: str) -> bool:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_key = :event_key"),
            {"project_id": project_id, "task_id": task_id, "event_key": f"prepared-{command_id}"},
        ).first()
    return found is not None


def _open_goal_gate(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    goal: str,
    approval: dict[str, Any] | None,
    command_id: str,
) -> dict[str, str]:
    digest = _publish_draft(database, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal)
    chosen = approval
    if chosen is None:
        chosen = _matching_approval(database, project_id=project_id, task_id=task_id, digest=digest)
    else:
        expected = str(chosen.get("expected_contract_sha256") or "")
        owner = str(chosen.get("owner_id") or "")
        if expected and expected != digest:
            return {"status": "hash_mismatch", "task_id": task_id}
        if not _is_project_owner(database, owner, project_id):
            return {"status": "ignored", "task_id": task_id}
    if chosen is None:
        actual = _set_status(
            database,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            status="WAITING_APPROVAL",
            allowed=("DRAFT", "QUEUED", "WAITING_APPROVAL"),
        )
        if actual == "WAITING_APPROVAL":
            return {"status": "waiting_approval", "task_id": task_id}
        return {"status": actual, "task_id": task_id}
    approver = str(chosen.get("owner_id") or "")
    if _command_was_prepared(database, owner_id, project_id, task_id, command_id):
        return {"status": "prepared", "task_id": task_id}
    state, _resume = _control_state(database, owner_id, project_id, task_id)
    if state not in {"QUEUED", "WAITING_APPROVAL", "RUNNING"}:
        return {"status": state, "task_id": task_id}
    if state == "RUNNING" and _load_authoritative_contract(database, owner_id, project_id, task_id) is not None:
        return {"status": state, "task_id": task_id}
    _ensure_contract(database, owner_id=owner_id, project_id=project_id, task_id=task_id, goal=goal, approved_by=approver)
    actual = _set_status(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        status="RUNNING",
        allowed=("QUEUED", "WAITING_APPROVAL", "RUNNING"),
        prepared_command_id=command_id,
    )
    if actual != "RUNNING":
        return {"status": actual, "task_id": task_id}
    return {"status": "prepared", "task_id": task_id}


def _resolve_review(database: Database, command: dict[str, Any], project_id: str, task_id: str) -> dict[str, str]:
    body = _command_body(command)
    decision = str(body.get("decision") or "")
    approval_id = str(body.get("approval_id") or "")
    owner_id = str(body.get("owner_id") or "")
    code = str(body.get("code") or "")
    from_status = str(body.get("from_status") or "")
    if decision not in {"approve", "reject"} or not approval_id or not _is_project_owner(database, owner_id, project_id):
        return {"status": "ignored", "task_id": task_id}
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT decision, owner_id::text AS owner_id, requested_payload FROM approvals "
                "WHERE project_id = :project_id AND id = :id AND task_id = :task_id"
            ),
            {"project_id": project_id, "id": approval_id, "task_id": task_id},
        ).mappings().first()
    if row is None or str(row["owner_id"]) != owner_id:
        return {"status": "ignored", "task_id": task_id}
    expected = "approved" if decision == "approve" else "rejected"
    payload = row["requested_payload"] if isinstance(row["requested_payload"], dict) else {}
    if str(row["decision"]) != expected or payload.get("action") != "resolve_review":
        return {"status": "ignored", "task_id": task_id}
    stage = from_status if from_status in {"QUEUED", "RUNNING", "VERIFYING"} else str(payload.get("from_status") or "")
    if stage not in {"QUEUED", "RUNNING", "VERIFYING"}:
        stage = "RUNNING"
    review_code = code or str(payload.get("code") or "")
    if review_code == "budget_unreconciled":
        _reconcile_overruns(database, owner_id, project_id, task_id, "confirm" if decision == "approve" else "release")
    if decision == "reject":
        actual = _set_status(
            database,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            status="CANCELLED",
            allowed=NON_TERMINAL_STATUSES,
            resume_state="cancel_requested",
        )
        BudgetLedger(database).revoke_unsent(owner_id=owner_id, project_id=project_id, task_id=task_id)
        return {"status": actual, "task_id": task_id}
    actual = _set_status(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        status=stage,
        allowed=("NEEDS_REVIEW",),
        clear_resume=True,
    )
    if actual != stage:
        return {"status": actual, "task_id": task_id}
    return {"status": "prepared", "task_id": task_id}


def _reconcile_overruns(database: Database, owner_id: str, project_id: str, task_id: str, decision: str) -> None:
    ledger = BudgetLedger(database)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        keys = conn.execute(
            text(
                "SELECT request_key FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                "AND usage_detail->>'book_result' = 'overrun' AND usage_detail->>'reconciliation' = 'unreconciled'"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).scalars().all()
    for request_key in keys:
        ledger.reconcile_overrun(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            request_key=str(request_key),
            decision=decision,
        )


def _frozen_contract(database: Database, *, owner_id: str, project_id: str, task_id: str, goal: str) -> dict[str, Any]:
    """Freeze the owner's draft criteria. Do not replace them with the automatic goal check."""
    draft = _load_draft(database, owner_id, project_id, task_id)
    # A published hash preview has no owner criteria. Only a draft that names criteria is frozen.
    if draft is None or "criteria" not in draft:
        return {"criterion_id": CRITERION_ID, "goal": goal}
    frozen: dict[str, Any] = {"goal": goal, "criteria": _freeze_criteria(draft, goal)}
    required = draft.get("required_artifacts")
    if isinstance(required, list) and required and all(isinstance(item, str) and item for item in required):
        frozen["required_artifacts"] = list(required)
    return frozen


def _load_draft(database: Database, owner_id: str, project_id: str, task_id: str) -> dict[str, Any] | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        raw = conn.execute(
            text("SELECT draft_contract FROM tasks WHERE project_id = :project_id AND id = :task_id"),
            {"project_id": project_id, "task_id": task_id},
        ).scalar()
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise RuntimeError("draft acceptance criteria are not an approved contract")
    return raw


def _freeze_criteria(draft: dict[str, Any], goal: str) -> list[dict[str, Any]]:
    draft_goal = draft.get("goal")
    if not isinstance(draft_goal, str) or draft_goal.strip() != goal.strip():
        raise RuntimeError("draft goal does not match the approved task")
    criteria = draft.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise RuntimeError("approved draft has no acceptance criteria")
    frozen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in criteria:
        if not _criterion_shape(item):
            raise RuntimeError("approved draft acceptance criteria are incomplete")
        criterion_id = str(item["criterion_id"])
        if criterion_id in seen:
            raise RuntimeError("approved draft repeats a criterion")
        seen.add(criterion_id)
        frozen.append(json.loads(json.dumps(item)))
    if not any(item.get("mandatory", True) is not False for item in frozen):
        raise RuntimeError("approved draft has no mandatory criterion")
    return frozen


def _criterion_shape(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    criterion_id = item.get("criterion_id")
    description = item.get("description")
    return isinstance(criterion_id, str) and bool(criterion_id) and item.get("template") in _CRITERION_TEMPLATES and isinstance(description, str) and bool(description)


def _load_authoritative_contract(database: Database, owner_id: str, project_id: str, task_id: str) -> dict[str, Any] | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        current = conn.execute(
            text("SELECT current_contract_id::text FROM tasks WHERE project_id = :project_id AND id = :task_id"),
            {"project_id": project_id, "task_id": task_id},
        ).scalar()
        if current:
            row = conn.execute(
                text(
                    "SELECT id::text AS id, content, contract_sha256 FROM goal_contracts "
                    "WHERE project_id = :project_id AND task_id = :task_id AND id = :contract_id"
                ),
                {"project_id": project_id, "task_id": task_id, "contract_id": current},
            ).mappings().first()
        else:
            row = conn.execute(
                text(
                    "SELECT id::text AS id, content, contract_sha256 FROM goal_contracts "
                    "WHERE project_id = :project_id AND task_id = :task_id "
                    "ORDER BY contract_version DESC LIMIT 1"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().first()
    if row is None:
        return None
    digest = sha256_text(canonical_json(row["content"]))
    content = row["content"]
    if isinstance(content, str):
        parsed = json.loads(content)
        content = parsed if isinstance(parsed, dict) else {}
    elif not isinstance(content, dict):
        content = {}
    return {"id": str(row["id"]), "digest": digest, "stored_digest": str(row["contract_sha256"]), "content": content}


def _ensure_artifact(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    classification: str,
    data: bytes,
    media_type: str,
) -> dict[str, str]:
    existing = _task_artifact(database, owner_id, project_id, task_id, classification)
    if existing is not None:
        return existing
    stored = store.put(owner_id=owner_id, project_id=project_id, data=data, media_type=media_type, classification=classification)
    _bind_task_artifact(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        artifact_id=str(stored["artifact_id"]),
        classification=classification,
    )
    return {"id": str(stored["artifact_id"]), "sha256": str(stored["sha256"])}


def _bind_task_artifact(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    artifact_id: str,
    classification: str,
) -> tuple[int, int]:
    """Link a task to a blob. A second task does not take over artifacts.task_id."""
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        updated = conn.execute(
            text(
                "UPDATE artifacts SET task_id = :task_id "
                "WHERE project_id = :project_id AND id = :artifact_id AND task_id IS NULL AND deleted_at IS NULL"
            ),
            {"task_id": task_id, "project_id": project_id, "artifact_id": artifact_id},
        ).rowcount
        inserted = conn.execute(
            text(
                "INSERT INTO task_artifacts (project_id, task_id, artifact_id, classification) "
                "VALUES (:project_id, :task_id, :artifact_id, :classification) "
                "ON CONFLICT (project_id, task_id, artifact_id, classification) WHERE deleted_at IS NULL "
                "DO NOTHING RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "artifact_id": artifact_id,
                "classification": classification,
            },
        ).scalar()
        linked = conn.execute(
            text(
                "SELECT 1 FROM task_artifacts WHERE project_id = :project_id AND task_id = :task_id "
                "AND artifact_id = :artifact_id AND classification = :classification AND deleted_at IS NULL"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "artifact_id": artifact_id,
                "classification": classification,
            },
        ).first()
    if linked is None:
        raise RuntimeError("task artifact link was not recorded")
    return int(updated or 0), 0 if inserted is None else 1


def _task_artifact(database: Database, owner_id: str, project_id: str, task_id: str, classification: str) -> dict[str, str] | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        linked = conn.execute(
            text(
                "SELECT a.id::text AS id, a.sha256 FROM task_artifacts AS link "
                "JOIN artifacts AS a ON a.project_id = link.project_id AND a.id = link.artifact_id "
                "WHERE link.project_id = :project_id AND link.task_id = :task_id AND link.classification = :classification "
                "AND link.deleted_at IS NULL AND a.deleted_at IS NULL "
                "ORDER BY link.created_at, link.id LIMIT 1"
            ),
            {"project_id": project_id, "task_id": task_id, "classification": classification},
        ).mappings().first()
        if linked is None:
            linked = conn.execute(
                text(
                    "SELECT id::text AS id, sha256 FROM artifacts "
                    "WHERE project_id = :project_id AND task_id = :task_id AND classification = :classification AND deleted_at IS NULL "
                    "ORDER BY created_at, id LIMIT 1"
                ),
                {"project_id": project_id, "task_id": task_id, "classification": classification},
            ).mappings().first()
    if linked is None:
        return None
    return {"id": str(linked["id"]), "sha256": str(linked["sha256"])}


def _ensure_verification(
    endpoints: WorkerEndpoints,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    contract_id: str,
    contract_sha: str,
    artifact_id: str,
    goal: str,
    allowed_ids: list[str] | None = None,
) -> str:
    if not endpoints.verifier_url:
        return verify_via_role(
            verifier_base=endpoints.verifier_base,
            token=endpoints.loopback_token,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            contract_id=contract_id,
            contract_sha=contract_sha,
            artifact_id=artifact_id,
            goal=goal,
            allowed_ids=allowed_ids,
        )
    verifier = Database(endpoints.verifier_url)
    store = ArtifactStore(endpoints.artifact_root, verifier)
    ledger, deleted = _artifact_ledger(verifier, owner_id, project_id, artifact_id)
    observed, readable = _observe_bytes(store, owner_id, project_id, artifact_id)
    sources, snapshot, source_problem = _seen_sources(verifier, store, owner_id, project_id, task_id, goal=goal, allowed_ids=allowed_ids)
    authoritative = _load_authoritative_contract(verifier, owner_id, project_id, task_id)
    contract_ok = (
        authoritative is not None
        and authoritative["id"] == contract_id
        and authoritative["digest"] == contract_sha
        and authoritative["stored_digest"] == authoritative["digest"]
    )
    byte_ok = (not deleted) and readable is not None and ledger is not None and observed == ledger
    if source_problem:
        goal_state = "unverifiable"
        goal_detail = source_problem
    else:
        goal_state = _goal_state(readable, goal, sources)
        goal_detail = _goal_detail(readable, goal, sources)
    answer_text = ""
    if readable is not None:
        try:
            answer_text = readable.decode("utf-8")
        except UnicodeError:
            answer_text = ""
    cited = any(cites_fact(answer_text, fact) for fact in required_facts(source_text="\n".join(sources), goal=goal))
    stored_artifact_sha = ledger or observed
    if not contract_ok:
        outcome = "fail"
        detail = "合同摘要与权威合同不一致"
    elif not byte_ok:
        outcome = "fail"
        detail = "字节完整性未通过"
    elif goal_state == "unverifiable":
        outcome = "unverifiable"
        if source_problem:
            detail = "模型当时看到的资料已无法原样核对"
        elif goal_detail == "conflicting_sources":
            detail = "资料之间对同一数值说法不一，不能确认答案"
        else:
            detail = "字节与合同可以通过，离线摘录不能确认目标完成"
    elif goal_state != "pass":
        outcome = "fail"
        detail = goal_detail or "回答没有覆盖目标"
    else:
        outcome = "pass"
        detail = "工作流检查、字节完整性和目标摘录都通过"
    acceptance_state = ""
    contract_content = authoritative.get("content") if isinstance(authoritative, dict) else None
    if outcome == "pass" and isinstance(contract_content, dict) and "criteria" in contract_content:
        acceptance_state, acceptance_detail = _acceptance_outcome(contract_content, answer=readable, sources=sources, goal=goal)
        if acceptance_state != "pass":
            outcome = acceptance_state
            detail = acceptance_detail
    if stored_artifact_sha != observed or deleted or not byte_ok:
        expected = "FAIL"
    elif outcome == "unverifiable":
        expected = "UNVERIFIABLE"
    elif outcome == "fail":
        expected = "FAIL"
    else:
        expected = "PASS"
    cached = _cached_verification(
        verifier,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        artifact_id=artifact_id,
        contract_id=contract_id,
        artifact_sha=stored_artifact_sha,
        contract_sha=contract_sha,
        observed=observed,
        snapshot=snapshot,
        expected=expected,
    )
    if cached is not None:
        return cached
    model_mode, offline = _generation_mode(verifier, owner_id, project_id, task_id, configured=endpoints.research_provider)
    report = {
        "criterion_id": CRITERION_ID,
        "artifact_id": artifact_id,
        "observed_sha256": observed,
        "ledger_sha256": ledger,
        "verifier_version": VERIFIER_VERSION,
        "model_mode": model_mode,
        "offline": offline,
        "pipeline": "pass",
        "byte_integrity": "pass" if byte_ok else "fail",
        "goal_completion": goal_state,
        "goal_failure": goal_detail,
        "citation": "pass" if cited else "fail",
        "contract_binding": "pass" if contract_ok else "fail",
    }
    if acceptance_state:
        report["acceptance_criteria"] = acceptance_state
    stored = store.put(
        owner_id=owner_id,
        project_id=project_id,
        data=json.dumps(report, sort_keys=True).encode(),
        media_type="text/plain",
        classification="task_verification_report",
    )
    lock_key = int.from_bytes(hashlib.sha256(f"{project_id}:{task_id}:{artifact_id}:{CRITERION_ID}".encode()).digest()[:8], "big", signed=True)
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        cached = _cached_verification_on(
            conn,
            project_id=project_id,
            task_id=task_id,
            artifact_id=artifact_id,
            contract_id=contract_id,
            artifact_sha=stored_artifact_sha,
            contract_sha=contract_sha,
            observed=observed,
            snapshot=snapshot,
            expected=expected,
        )
        if cached is not None:
            return cached
        now = datetime.now(UTC)
        recorded = record_verification(
            verifier,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            contract_id=contract_id,
            artifact_id=artifact_id,
            report_artifact_id=str(stored["artifact_id"]),
            criterion_id=CRITERION_ID,
            artifact_sha256=stored_artifact_sha,
            observed_sha256=observed,
            contract_sha256=contract_sha,
            expires_at=now + timedelta(hours=24),
            now=now,
            checks_passed=outcome != "fail",
            outcome=outcome,
            detail=detail,
            source_snapshot_sha256=snapshot,
            verifier_version=VERIFIER_VERSION,
            conn=conn,
        )
    return str(recorded["id"])


def _cached_verification(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    artifact_id: str,
    contract_id: str,
    artifact_sha: str,
    contract_sha: str,
    observed: str,
    snapshot: str,
    expected: str,
) -> str | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return _cached_verification_on(
            conn,
            project_id=project_id,
            task_id=task_id,
            artifact_id=artifact_id,
            contract_id=contract_id,
            artifact_sha=artifact_sha,
            contract_sha=contract_sha,
            observed=observed,
            snapshot=snapshot,
            expected=expected,
        )


def _cached_verification_on(
    conn: Any,
    *,
    project_id: str,
    task_id: str,
    artifact_id: str,
    contract_id: str,
    artifact_sha: str,
    contract_sha: str,
    observed: str,
    snapshot: str,
    expected: str,
) -> str | None:
    found = conn.execute(
        text(
            "SELECT id::text FROM verification_results "
            "WHERE project_id = :project_id AND task_id = :task_id AND criterion_id = :criterion_id "
            "AND artifact_id = :artifact_id AND contract_id = :contract_id "
            "AND artifact_sha256 = :artifact_sha AND contract_sha256 = :contract_sha "
            "AND observed_sha256 = :observed AND source_snapshot_sha256 = :snapshot "
            "AND verifier_version = :verifier_version AND verdict = :expected "
            "ORDER BY created_at, id LIMIT 1"
        ),
        {
            "project_id": project_id,
            "task_id": task_id,
            "criterion_id": CRITERION_ID,
            "artifact_id": artifact_id,
            "contract_id": contract_id,
            "artifact_sha": artifact_sha,
            "contract_sha": contract_sha,
            "observed": observed,
            "snapshot": snapshot,
            "verifier_version": VERIFIER_VERSION,
            "expected": expected,
        },
    ).scalar()
    return None if found is None else str(found)


def _source_snapshot(
    database: Database,
    store: ArtifactStore,
    owner_id: str,
    project_id: str,
    *,
    allowed_ids: list[str] | None = None,
) -> tuple[list[str], str, str, list[str]]:
    texts, _manifest, snapshot, state, missing = _read_sources(database, store, owner_id, project_id, allowed_ids=allowed_ids)
    return texts, snapshot, state, missing


def _manifest_sha(manifest: list[dict[str, str]]) -> str:
    return sha256_text("\n".join(f"{entry['id']}:{entry['sha256']}" for entry in manifest))


def _read_sources(
    database: Database,
    store: ArtifactStore,
    owner_id: str,
    project_id: str,
    *,
    allowed_ids: list[str] | None = None,
) -> tuple[list[str], list[dict[str, str]], str, str, list[str]]:
    """Source texts plus the manifest of artifact hashes the snapshot hash is computed from."""
    if allowed_ids is not None and len(allowed_ids) == 0:
        return [], [], sha256_text(""), "empty", []
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        if allowed_ids is None:
            rows = conn.execute(
                text(
                    "SELECT id::text AS id, sha256 FROM artifacts "
                    "WHERE project_id = :project_id AND deleted_at IS NULL "
                    "AND media_type = 'text/plain' AND classification = 'project_private' "
                    "ORDER BY created_at, id"
                ),
                {"project_id": project_id},
            ).mappings().all()
        else:
            rows = []
            for item in allowed_ids:
                row = conn.execute(
                    text(
                        "SELECT id::text AS id, sha256, media_type, classification, task_id::text AS task_id, deleted_at "
                        "FROM artifacts WHERE project_id = :project_id AND id = :id"
                    ),
                    {"project_id": project_id, "id": item},
                ).mappings().first()
                if row is None or row["deleted_at"] is not None or row["media_type"] != "text/plain" or row["classification"] != "project_private":
                    return [], [], sha256_text("invalid"), "invalid", [str(item)]
                rows.append(row)
    texts: list[str] = []
    manifest: list[dict[str, str]] = []
    explicit = allowed_ids is not None
    for row in rows:
        artifact_id = str(row["id"])
        try:
            data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
        except (ArtifactError, OSError):
            if explicit:
                return [], [], sha256_text(f"invalid:{artifact_id}"), "invalid", [artifact_id]
            manifest.append({"id": artifact_id, "sha256": "unreadable"})
            continue
        manifest.append({"id": artifact_id, "sha256": str(row["sha256"])})
        try:
            texts.append(data.decode("utf-8"))
        except UnicodeError:
            if explicit:
                return [], [], sha256_text(f"invalid:{artifact_id}"), "invalid", [artifact_id]
            continue
    return texts, manifest, _manifest_sha(manifest), "ok", []


def _seen_sources(
    database: Database,
    store: ArtifactStore,
    owner_id: str,
    project_id: str,
    task_id: str,
    *,
    goal: str,
    allowed_ids: list[str] | None,
) -> tuple[list[str], str, str]:
    """Re-read the sources recorded for this task's generation. Returns texts, snapshot and a problem code.

    The current project may have gained or lost sources since the model was
    called. Only the recorded manifest is evidence for the answer.
    """
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        raw = conn.execute(
            text("SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"),
            {"project_id": project_id, "task_id": task_id, "request_key": f"research-{task_id}"},
        ).scalar()
    detail = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
    manifest = detail.get("source_manifest")
    if not isinstance(manifest, list) or not manifest or not all(isinstance(entry, dict) and entry.get("id") and entry.get("sha256") for entry in manifest):
        return [], sha256_text("unrecorded"), "source_manifest_missing"
    snapshot = _manifest_sha(manifest)
    if detail.get("source_snapshot_sha256") != snapshot:
        return [], snapshot, "source_manifest_mismatch"
    if allowed_ids is not None and any(str(entry["id"]) not in allowed_ids for entry in manifest):
        return [], snapshot, "source_outside_allowed"
    texts: list[str] = []
    changed: list[str] = []
    for entry in manifest:
        artifact_id = str(entry["id"])
        expected = str(entry["sha256"])
        if expected == "unreadable":
            continue
        ledger, deleted = _artifact_ledger(database, owner_id, project_id, artifact_id)
        observed, data = _observe_bytes(store, owner_id, project_id, artifact_id)
        if deleted or data is None or ledger != expected or observed != expected:
            changed.append(artifact_id)
            continue
        try:
            texts.append(data.decode("utf-8"))
        except UnicodeError:
            continue
    if changed:
        return [], snapshot, "source_changed:" + ",".join(changed)
    prompt_sha = detail.get("prompt_sha256")
    if prompt_sha is not None and prompt_sha != sha256_text(research_prompt(goal=goal, sources=texts)):
        return [], snapshot, "source_prompt_mismatch"
    return texts, snapshot, ""


def _missing_live_settings(endpoints: WorkerEndpoints) -> list[str]:
    missing = []
    if not endpoints.live_model_base:
        missing.append("AETHER_LIVE_MODEL_BASE")
    if not endpoints.live_model_alias:
        missing.append("AETHER_LIVE_MODEL_ALIAS")
    if not endpoints.live_model_key and not endpoints.gateway_base:
        missing.append("AETHER_LIVE_MODEL_KEY")
    return missing


def _needs_review(database: Database, *, owner_id: str, project_id: str, task_id: str, reason: str, code: str, missing: list[str] | None = None) -> dict[str, str]:
    state, resume = _control_state(database, owner_id, project_id, task_id)
    if resume in {"pause_requested", "cancel_requested"} or state == "PAUSED" or state in TERMINAL_STATUSES:
        return {"status": state, "task_id": task_id, "stop_reason": code}
    origin = state if state in {"QUEUED", "RUNNING", "VERIFYING"} else ""
    if resume.startswith("review:"):
        recorded = resume.split(":", 1)[1]
        if recorded in {"QUEUED", "RUNNING", "VERIFYING"}:
            origin = recorded
    if origin not in {"QUEUED", "RUNNING", "VERIFYING"}:
        origin = "RUNNING"
    actual = _set_status(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        status="NEEDS_REVIEW",
        allowed=("QUEUED", "RUNNING", "VERIFYING"),
        resume_state=None if resume.startswith("review:") else f"review:{origin}",
    )
    _event(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        event_key=f"needs-review-{code}-{task_id}",
        event_type="task.needs_review",
        payload={"reason": reason, "code": code, "missing": missing or [], "from_status": origin},
    )
    if actual == "NEEDS_REVIEW":
        _ensure_review_approval(database, owner_id=owner_id, project_id=project_id, task_id=task_id, from_status=origin, code=code, reason=reason)
    return {"status": actual, "task_id": task_id, "stop_reason": code}


def _ensure_review_approval(database: Database, *, owner_id: str, project_id: str, task_id: str, from_status: str, code: str, reason: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text(
                "SELECT id FROM approvals WHERE project_id = :project_id AND task_id = :task_id "
                "AND decision = 'pending' AND expires_at > now() "
                "AND requested_payload->>'action' = 'resolve_review' AND requested_payload->>'code' = :code "
                "LIMIT 1"
            ),
            {"project_id": project_id, "task_id": task_id, "code": code},
        ).first()
    if existing is not None:
        return
    OperationLedger(database).request_approval(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        intent={"action": "resolve_review", "task_id": task_id, "from_status": from_status, "code": code, "reason": reason},
        risk="R2",
        ttl_seconds=REVIEW_TTL_SECONDS,
    )


def _task_start_payload(database: Database, project_id: str, task_id: str) -> dict[str, Any]:
    with database.transaction(project_id=project_id) as conn:
        payload = conn.execute(
            text(
                "SELECT payload FROM command_outbox WHERE project_id = :project_id AND task_id = :id AND kind = 'start' "
                "ORDER BY created_at LIMIT 1"
            ),
            {"project_id": project_id, "id": task_id},
        ).scalar()
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        loaded = json.loads(payload)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def request_cost_ceiling(*, input_tokens: int, output_tokens: int) -> int:
    """Worst-case micro-USD for one bounded request before it is sent."""
    return input_tokens * INPUT_MICRO_USD_PER_TOKEN + output_tokens * OUTPUT_MICRO_USD_PER_TOKEN


def _live_dispatch_plan(*, prompt: str, tokens_left: int, cost_left: int) -> dict[str, int] | None:
    """Refuse the call when even one output token cannot fit the remaining budget.

    A full 256-token output reservation is not required. The cap shrinks to the
    output the remaining money can still cover.
    """
    input_estimate = estimate_provider_request_tokens(prompt)
    output_cap = min(OUTPUT_TOKEN_CAP, max(0, tokens_left - input_estimate))
    input_cost = input_estimate * INPUT_MICRO_USD_PER_TOKEN
    if output_cap < 1 or cost_left <= input_cost:
        return None
    affordable_output = (cost_left - input_cost) // OUTPUT_MICRO_USD_PER_TOKEN
    output_cap = min(output_cap, int(affordable_output))
    amount = request_cost_ceiling(input_tokens=input_estimate, output_tokens=output_cap)
    if output_cap < 1 or amount < 1 or cost_left < amount:
        return None
    return {"output_cap": output_cap, "amount": amount, "tokens": input_estimate + output_cap}


def _budget_from_payload(value: Any) -> dict[str, Any]:
    spec = dict(DEFAULT_BUDGET)
    if isinstance(value, dict):
        for key in spec:
            if key in value:
                spec[key] = value[key]
    spec["verification_reserve_fraction"] = float(spec["verification_reserve_fraction"])
    return spec


def _mark_budget_exhausted(database: Database, *, owner_id: str, project_id: str, task_id: str, limit: str) -> dict[str, str]:
    actual = _set_status(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        status="FAILED",
        allowed=("QUEUED", "RUNNING", "VERIFYING"),
        resume_state="budget_exhausted",
    )
    _event(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        event_key=f"budget-{task_id}",
        event_type="task.budget_exhausted",
        payload={"reason": "预算已用尽", "code": "budget_exhausted", "limit": limit},
    )
    return {"status": actual, "task_id": task_id, "stop_reason": "budget_exhausted"}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _active_elapsed_seconds(conn: Any, project_id: str, task_id: str) -> float:
    """Sum RUNNING and VERIFYING spans. Queue, approval, pause, and review stay outside the sum."""
    rows = conn.execute(
        text(
            "SELECT created_at, payload FROM task_events "
            "WHERE project_id = :project_id AND task_id = :task_id AND event_type = 'task.active' "
            "ORDER BY sequence"
        ),
        {"project_id": project_id, "task_id": task_id},
    ).mappings().all()
    total = 0.0
    opened: datetime | None = None
    now = datetime.now(UTC)
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"]) if isinstance(row["payload"], str) else {}
        phase = str(payload.get("phase") or "")
        created = _aware(row["created_at"])
        if phase == "start":
            opened = created
        elif phase == "stop" and opened is not None:
            total += max(0.0, (created - opened).total_seconds())
            opened = None
    if opened is not None:
        total += max(0.0, (now - opened).total_seconds())
    return total


def _deadline_exceeded_on(conn: Any, project_id: str, task_id: str) -> bool:
    active = conn.execute(
        text("SELECT active_seconds FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id"),
        {"project_id": project_id, "task_id": task_id},
    ).scalar()
    if active is None:
        return False
    return _active_elapsed_seconds(conn, project_id, task_id) > int(active)


def _past_deadline(database: Database, owner_id: str, project_id: str, task_id: str) -> bool:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return _deadline_exceeded_on(conn, project_id, task_id)


def _task_age_seconds(database: Database, owner_id: str, project_id: str, task_id: str) -> float:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return _active_elapsed_seconds(conn, project_id, task_id)


def _active_time_exhausted(database: Database, owner_id: str, project_id: str, task_id: str) -> bool:
    payload = _task_start_payload(database, project_id, task_id)
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else None
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        if _deadline_exceeded_on(conn, project_id, task_id):
            return True
        if budget is None or budget.get("active_seconds") is None:
            return False
        return _active_elapsed_seconds(conn, project_id, task_id) > int(budget["active_seconds"])


def _book_truncated_call(
    ledger: BudgetLedger,
    result: dict[str, Any],
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
) -> None:
    """Record the spent call. The cut text is not stored as the answer."""
    input_tokens = result.get("input_tokens")
    output_tokens = result.get("output_tokens")
    if result.get("usage_status") == "unknown" or input_tokens is None or output_tokens is None:
        ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key)
        return
    ledger.record_model_response(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
        output="",
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cost_micro_usd=None if result.get("actual_cost_micro_usd") is None else int(result["actual_cost_micro_usd"]),
        upstream_model=None if result.get("upstream_model") is None else str(result["upstream_model"]),
        requested_alias=None if result.get("requested_alias") is None else str(result["requested_alias"]),
        cost_source=str(result.get("cost_source") or "unreported"),
        pricing_version=None if result.get("pricing_version") is None else str(result["pricing_version"]),
        finish_reason="length",
    )


def _ensure_live_answer(
    endpoints: WorkerEndpoints,
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    goal: str,
    budget: Any,
    allowed_ids: list[str] | None = None,
) -> str | dict[str, str] | None:
    existing = _task_artifact(database, owner_id, project_id, task_id, "task_answer")
    if existing is not None:
        if _past_deadline(database, owner_id, project_id, task_id):
            return _mark_budget_exhausted(database, owner_id=owner_id, project_id=project_id, task_id=task_id, limit="active_seconds")
        return existing["id"]
    if _block_execution(database, owner_id, project_id, task_id) is not None:
        return None
    fresh, manifest, snapshot_sha, fresh_state, missing_sources = _read_sources(database, store, owner_id, project_id, allowed_ids=allowed_ids)
    if fresh_state == "empty":
        return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="允许的来源是空的", code="allowed_sources_empty")
    if fresh_state != "ok":
        return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="允许的来源不合法或不可读", code="allowed_sources_invalid", missing=missing_sources)
    spec = _budget_from_payload(budget)
    ledger = BudgetLedger(database)
    try:
        ledger.open_account(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            limit_micro_usd=int(spec["max_cost_micro_usd"]),
            token_limit=int(spec["max_tokens"]),
            max_requests=int(spec["max_model_requests"]),
            verification_reserve_fraction=float(spec["verification_reserve_fraction"]),
            active_seconds=int(spec["active_seconds"]),
        )
    except IntegrityError:
        pass
    age = _task_age_seconds(database, owner_id, project_id, task_id)
    if age > int(spec["active_seconds"]):
        return _mark_budget_exhausted(database, owner_id=owner_id, project_id=project_id, task_id=task_id, limit="active_seconds")
    request_key = f"research-{task_id}"
    attempt_id = uuid.uuid4().hex
    account = ledger.snapshot(owner_id=owner_id, project_id=project_id, task_id=task_id, reclaimable_request_key=request_key)
    fraction = float(spec["verification_reserve_fraction"])
    token_ceiling = int(int(spec["max_tokens"]) * (1 - fraction))
    cost_ceiling = int(int(spec["max_cost_micro_usd"]) * (1 - fraction))
    tokens_left = token_ceiling - int(account["tokens_used"]) - int(account["tokens_held"])
    cost_left = cost_ceiling - int(account["actual_known_micro_usd"]) - int(account["held_micro_usd"])
    prompt = research_prompt(goal=goal, sources=fresh)
    plan = _live_dispatch_plan(prompt=prompt, tokens_left=tokens_left, cost_left=cost_left)
    if plan is None:
        return _mark_budget_exhausted(database, owner_id=owner_id, project_id=project_id, task_id=task_id, limit="reservation")
    output_cap = int(plan["output_cap"])
    prompt_sha = sha256_text(prompt)
    prepared = ledger.prepare_model_call(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
        amount=int(plan["amount"]),
        role="research",
        tokens=int(plan["tokens"]),
        model_alias=endpoints.live_model_alias,
        prompt_sha256=prompt_sha,
        output_cap=output_cap,
        source_snapshot_sha256=snapshot_sha,
        source_manifest=manifest,
        attempt_id=attempt_id,
    )
    action = str(prepared["action"])
    if action == "denied":
        return _mark_budget_exhausted(database, owner_id=owner_id, project_id=project_id, task_id=task_id, limit="reservation")
    if action in {"unreconciled", "conflict"}:
        return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型调用结果未能对账", code="live_call_unreconciled")
    if action == "send":
        remaining = float(spec["active_seconds"]) - _task_age_seconds(database, owner_id, project_id, task_id)
        held = {"owner_id": owner_id, "project_id": project_id, "task_id": task_id, "request_key": request_key, "attempt_id": attempt_id}
        if remaining <= 0:
            ledger.release_unsent(**held)
            return _mark_budget_exhausted(database, owner_id=owner_id, project_id=project_id, task_id=task_id, limit="active_seconds")
        gateway = ModelGateway(
            database,
            upstream_base=endpoints.live_model_base,
            provider_key=endpoints.live_model_key,
            timeout=min(float(endpoints.live_timeout_seconds), remaining),
        )
        token = gateway.issue_token(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            request_key=request_key,
            attempt_id=attempt_id,
            capabilities=["knowledge:read"],
            aliases=[endpoints.live_model_alias],
        )
        blocked = _block_execution(database, owner_id, project_id, task_id)
        if blocked is not None:
            ledger.release_unsent(**held)
            return {"status": blocked, "task_id": task_id}
        if not ledger.acquire_dispatch_permit(**held):
            ledger.release_unsent(**held)
            return {"status": _task_status(database, owner_id, project_id, task_id), "task_id": task_id}
        if not ledger.consume_dispatch_permit(**held):
            ledger.release_unsent(**held)
            return {"status": _task_status(database, owner_id, project_id, task_id), "task_id": task_id}
        try:
            result = _complete_model(endpoints, gateway, token, alias=endpoints.live_model_alias, prompt=prompt, output_cap=output_cap)
        except GatewayDenied as denied:
            if not denied.sent:
                ledger.release_refused_send(**held, code=denied.code)
                blocked = _block_execution(database, owner_id, project_id, task_id)
                if blocked is not None:
                    return {"status": blocked, "task_id": task_id}
                return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型网关拒绝了这次调用", code="gateway_denied")
            ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key)
            return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型上游失败", code="upstream_error")
        if result.get("finish_reason") == "length":
            _book_truncated_call(
                ledger,
                result,
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                request_key=request_key,
            )
            return _needs_review(
                database,
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                reason=TRUNCATED_ANSWER,
                code="output_truncated",
            )
        if result.get("usage_status") == "unknown" and not result.get("output"):
            ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key)
            return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型上游超时", code="upstream_timeout")
        output = str(result.get("output") or "").strip()
        input_tokens = result.get("input_tokens")
        output_tokens = result.get("output_tokens")
        book_result = ledger.record_model_response(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            request_key=request_key,
            output=output,
            input_tokens=None if input_tokens is None else int(input_tokens),
            output_tokens=None if output_tokens is None else int(output_tokens),
            cost_micro_usd=None if result.get("actual_cost_micro_usd") is None else int(result["actual_cost_micro_usd"]),
            upstream_model=None if result.get("upstream_model") is None else str(result["upstream_model"]),
            requested_alias=None if result.get("requested_alias") is None else str(result["requested_alias"]),
            cost_source=str(result.get("cost_source") or "unreported"),
            pricing_version=None if result.get("pricing_version") is None else str(result["pricing_version"]),
        )
        if not output:
            if book_result == "overrun":
                return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型回报超出预留，需要对账", code="budget_unreconciled")
            return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型没有返回可用回答", code="empty_answer")
        recorded = {
            "output": output,
            "reported_input_tokens": input_tokens,
            "reported_output_tokens": output_tokens,
            "reported_cost_micro_usd": result.get("actual_cost_micro_usd"),
            "cost_source": str(result.get("cost_source") or "unreported") if result.get("actual_cost_micro_usd") is not None else "unreported",
            "pricing_version": result.get("pricing_version") if result.get("actual_cost_micro_usd") is not None else None,
            "upstream_model": result.get("upstream_model"),
            "requested_alias": result.get("requested_alias"),
            "book_result": book_result,
            "reconciliation": "unreconciled" if book_result == "overrun" else "booked",
        }
    else:
        recorded = dict(prepared.get("response") or {})
        if recorded.get("finish_reason") == "length":
            return _needs_review(
                database,
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                reason=TRUNCATED_ANSWER,
                code="output_truncated",
            )
        book_result = str(prepared.get("book_result") or recorded.get("book_result") or "replayed")
        output = str(recorded.get("output") or "").strip()
        if not output:
            return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型调用结果未能对账", code="live_call_unreconciled")
    if book_result == "overrun" and str(recorded.get("reconciliation") or "unreconciled") != "confirmed":
        return _needs_review(database, owner_id=owner_id, project_id=project_id, task_id=task_id, reason="模型回报超出预留，需要对账", code="budget_unreconciled")
    if _task_age_seconds(database, owner_id, project_id, task_id) > int(spec["active_seconds"]):
        return _mark_budget_exhausted(database, owner_id=owner_id, project_id=project_id, task_id=task_id, limit="active_seconds")
    blocked = _block_execution(database, owner_id, project_id, task_id)
    if blocked is not None:
        return {"status": blocked, "task_id": task_id}
    stored = store.put(owner_id=owner_id, project_id=project_id, data=output.encode(), media_type="text/plain", classification="task_answer")
    _bind_task_artifact(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        artifact_id=str(stored["artifact_id"]),
        classification="task_answer",
    )
    _event(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        event_key=f"research-{task_id}",
        event_type="research.call",
        payload={
            "calls": 1,
            "model_mode": "live",
            "offline": False,
            "requested_alias": recorded.get("requested_alias"),
            "upstream_model": recorded.get("upstream_model"),
            "input_tokens": recorded.get("reported_input_tokens"),
            "output_tokens": recorded.get("reported_output_tokens"),
            "cost_micro_usd": recorded.get("reported_cost_micro_usd"),
            "cost_status": "estimated" if recorded.get("cost_source") == "local_price_estimate" else "unknown",
            "cost_source": recorded.get("cost_source"),
            "pricing_version": recorded.get("pricing_version"),
            "usage_status": "known" if book_result == "settled" else "unknown",
        },
    )
    return str(stored["artifact_id"])


def _remember_model_call(database: Database, *, owner_id: str, project_id: str, task_id: str, request_key: str, result: dict[str, Any]) -> None:
    detail = {
        "requested_alias": result.get("requested_alias"),
        "upstream_model": result.get("upstream_model"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "cost_status": result.get("cost_status") or "unknown",
    }
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE model_requests SET model_alias = :alias, model_revision = :upstream, usage_detail = COALESCE(usage_detail, '{}'::jsonb) || CAST(:detail AS jsonb) "
                "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
            ),
            {
                "alias": str(result.get("requested_alias") or ""),
                "upstream": result.get("upstream_model"),
                "detail": json.dumps(detail),
                "project_id": project_id,
                "task_id": task_id,
                "request_key": request_key,
            },
        )


def _ensure_answer(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    goal: str,
    sources: list[str],
    manifest: list[dict[str, str]],
) -> str | None:
    existing = _task_artifact(database, owner_id, project_id, task_id, "task_answer")
    if existing is not None:
        return existing["id"]
    if _block_execution(database, owner_id, project_id, task_id) is not None:
        return None
    output = scripted_answer(source_text="\n".join(sources), goal=goal)
    if not output:
        return ""
    if _block_execution(database, owner_id, project_id, task_id) is not None:
        return None
    _record_scripted_generation(database, owner_id=owner_id, project_id=project_id, task_id=task_id, manifest=manifest)
    stored = store.put(owner_id=owner_id, project_id=project_id, data=output.encode(), media_type="text/plain", classification="task_answer")
    if _block_execution(database, owner_id, project_id, task_id) is not None:
        return None
    _bind_task_artifact(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        artifact_id=str(stored["artifact_id"]),
        classification="task_answer",
    )
    _event(
        database,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        event_key=f"research-{task_id}",
        event_type="research.call",
        payload={"calls": 1, "model_mode": "scripted", "offline": True},
    )
    return str(stored["artifact_id"])


def _record_scripted_generation(database: Database, *, owner_id: str, project_id: str, task_id: str, manifest: list[dict[str, str]]) -> None:
    """The scripted answer gets the same generation row a live call has, at zero cost."""
    detail = {"model_mode": "scripted", "source_manifest": manifest, "source_snapshot_sha256": _manifest_sha(manifest)}
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status, usage_detail) "
                "VALUES (:project_id, :task_id, :request_key, :digest, 'research', 'scripted', 0, 'scripted', CAST(:detail AS jsonb)) "
                "ON CONFLICT (project_id, task_id, request_key) DO NOTHING"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "request_key": f"research-{task_id}",
                "digest": sha256_text(canonical_json(detail)),
                "detail": json.dumps(detail),
            },
        )


def _generation_mode(database: Database, owner_id: str, project_id: str, task_id: str, *, configured: str) -> tuple[str, bool]:
    """The mode of the recorded generation; the configured provider only when nothing was recorded."""
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        status = conn.execute(
            text("SELECT usage_status FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"),
            {"project_id": project_id, "task_id": task_id, "request_key": f"research-{task_id}"},
        ).scalar()
    scripted = status == "scripted" if status is not None else configured == "scripted"
    return ("scripted", True) if scripted else ("live", False)


def _answer_text(data: bytes | None) -> str:
    if data is None:
        return ""
    try:
        return data.decode("utf-8").strip()
    except UnicodeError:
        return ""


def _acceptance_outcome(content: dict[str, Any], *, answer: bytes | None, sources: list[str], goal: str) -> tuple[str, str]:
    if "criteria" not in content:
        return "pass", ""
    criteria = content.get("criteria")
    if not isinstance(criteria, list) or not criteria or any(not isinstance(item, dict) for item in criteria):
        return "unverifiable", "冻结的验收条款不可读"
    mandatory = [item for item in criteria if item.get("mandatory", True) is not False]
    if not mandatory:
        return "unverifiable", "冻结的验收条款没有强制项"
    states = [_one_criterion(item, answer=answer, sources=sources, goal=goal) for item in mandatory]
    if any(state == "fail" for state in states):
        return "fail", "用户批准的验收条款未满足"
    if any(state != "pass" for state in states):
        return "unverifiable", "用户批准的验收条款不能用自动目标条款代替"
    return "pass", ""


def _one_criterion(item: dict[str, Any], *, answer: bytes | None, sources: list[str], goal: str) -> str:
    template = item.get("template")
    parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    if template == "artifact_exists":
        return _artifact_exists_criterion(parameters, answer)
    if template == "citation_integrity":
        return _citation_criterion(parameters, answer, sources, goal)
    if template == "source_coverage":
        return _source_coverage_criterion(parameters, sources)
    return "unverifiable"


def _artifact_exists_criterion(parameters: dict[str, Any], answer: bytes | None) -> str:
    logical = parameters.get("logical_name")
    minimum = parameters.get("minimum_bytes")
    if not isinstance(logical, str) or not logical.strip():
        return "fail"
    if type(minimum) is not int or minimum < 1:
        return "fail"
    if answer is None or len(answer) < minimum:
        return "fail"
    return "pass"


def _citation_criterion(parameters: dict[str, Any], answer: bytes | None, sources: list[str], goal: str) -> str:
    minimum = parameters.get("min_citations")
    if parameters.get("require_all_valid") is not True:
        return "fail"
    if type(minimum) is not int or minimum < 1 or answer is None:
        return "fail"
    try:
        text_value = answer.decode("utf-8")
    except UnicodeError:
        return "fail"
    facts = required_facts(source_text="\n".join(sources), goal=goal)
    cited = [fact for fact in facts if cites_fact(text_value, fact)]
    if len(cited) < minimum or len(cited) != len(facts):
        return "fail"
    return "pass"


def _source_coverage_criterion(parameters: dict[str, Any], sources: list[str]) -> str:
    minimum = parameters.get("minimum_distinct_sources")
    required = parameters.get("required_source_ids")
    if type(minimum) is not int or minimum < 1 or not isinstance(required, list):
        return "fail"
    if any(not isinstance(item, str) for item in required) or required:
        return "unverifiable"
    distinct = {item.strip() for item in sources if item.strip()}
    if len(distinct) < minimum:
        return "fail"
    return "pass"


def _goal_state(data: bytes | None, goal: str, sources: list[str]) -> str:
    if data is None:
        return "fail"
    try:
        data.decode("utf-8")
    except UnicodeError:
        return "fail"
    return goal_completion_state(answer=_answer_text(data), goal=goal, source_text="\n".join(sources))


def _goal_detail(data: bytes | None, goal: str, sources: list[str]) -> str:
    if data is None:
        return "empty_answer"
    try:
        data.decode("utf-8")
    except UnicodeError:
        return "unreadable_answer"
    return goal_completion_detail(answer=_answer_text(data), goal=goal, source_text="\n".join(sources))


def _artifact_ledger(database: Database, owner_id: str, project_id: str, artifact_id: str) -> tuple[str | None, bool]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT sha256, deleted_at FROM artifacts WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": artifact_id},
        ).mappings().first()
    if row is None:
        return None, True
    return str(row["sha256"]), row["deleted_at"] is not None


def _observe_bytes(store: ArtifactStore, owner_id: str, project_id: str, artifact_id: str) -> tuple[str, bytes | None]:
    try:
        data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
    except ArtifactError:
        return hashlib.sha256(b"unreadable").hexdigest(), None
    return hashlib.sha256(data).hexdigest(), data


def _task_status(database: Database, owner_id: str, project_id: str, task_id: str) -> str:
    state, _resume = _control_state(database, owner_id, project_id, task_id)
    return state


def _control_state(database: Database, owner_id: str, project_id: str, task_id: str) -> tuple[str, str]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT status, resume_state FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).mappings().one()
    return str(row["status"]), str(row["resume_state"] or "")


def _complete_model(endpoints: WorkerEndpoints, gateway: ModelGateway, token: str, *, alias: str, prompt: str, output_cap: int) -> dict[str, Any]:
    body = {"alias": alias, "prompt": prompt, "max_tokens": output_cap}
    if endpoints.live_model_key or not endpoints.gateway_base:
        return gateway.complete(token, body)
    return complete_via_gateway(
        gateway_base=endpoints.gateway_base,
        token=endpoints.loopback_token,
        call_token=token,
        alias=alias,
        prompt=prompt,
        max_tokens=output_cap,
        timeout=gateway.timeout,
    )


def _verdict(endpoints: WorkerEndpoints, owner_id: str, project_id: str, verification_id: str) -> str:
    verifier = Database(endpoints.verifier_url or endpoints.worker_url)
    with verifier.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return str(conn.execute(text("SELECT verdict FROM verification_results WHERE id = :id"), {"id": verification_id}).scalar_one())


def _block_execution(database: Database, owner_id: str, project_id: str, task_id: str) -> str | None:
    state, resume = _control_state(database, owner_id, project_id, task_id)
    if resume == "cancel_requested" and state not in TERMINAL_STATUSES:
        return _set_status(
            database,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            status="CANCELLED",
            allowed=NON_TERMINAL_STATUSES,
            resume_state="cancel_requested",
        )
    if state in STOP_STATUSES:
        return state
    if resume == "pause_requested":
        return _set_status(
            database,
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            status="PAUSED",
            allowed=PAUSEABLE_STATUSES,
            resume_state="pause_requested",
        )
    return None


def _commit_terminal(database: Database, *, owner_id: str, project_id: str, task_id: str, status: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT status, resume_state FROM tasks WHERE project_id = :project_id AND id = :id FOR UPDATE"),
            {"project_id": project_id, "id": task_id},
        ).mappings().one()
        state = str(row["status"])
        resume = str(row["resume_state"] or "")
        if state in TERMINAL_STATUSES:
            return state
        if resume == "cancel_requested":
            updated = _update_status(conn, project_id=project_id, task_id=task_id, status="CANCELLED", allowed=NON_TERMINAL_STATUSES, resume_state="cancel_requested")
            return updated or state
        if state == "PAUSED" or resume == "pause_requested":
            if state != "PAUSED":
                updated = _update_status(conn, project_id=project_id, task_id=task_id, status="PAUSED", allowed=PAUSEABLE_STATUSES, resume_state="pause_requested")
                return updated or state
            return state
        commit_status = status
        resume_for_commit = None
        if status == "SUCCEEDED" and _deadline_exceeded_on(conn, project_id, task_id):
            commit_status = "FAILED"
            resume_for_commit = "budget_exhausted"
        updated = _update_status(
            conn,
            project_id=project_id,
            task_id=task_id,
            status=commit_status,
            allowed=("VERIFYING", "RUNNING"),
            resume_state=resume_for_commit,
        )
        if updated is not None:
            return updated
        current = conn.execute(
            text("SELECT status FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).scalar_one()
    return str(current)


def _set_status(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    status: str,
    allowed: tuple[str, ...],
    resume_state: str | None = None,
    clear_resume: bool = False,
    prepared_command_id: str | None = None,
) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        updated = _update_status(
            conn,
            project_id=project_id,
            task_id=task_id,
            status=status,
            allowed=allowed,
            resume_state=resume_state,
            clear_resume=clear_resume,
            prepared_command_id=prepared_command_id,
        )
        if updated is not None:
            return updated
        current = conn.execute(
            text("SELECT status FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).scalar_one()
    return str(current)


def _update_status(
    conn: Any,
    *,
    project_id: str,
    task_id: str,
    status: str,
    allowed: tuple[str, ...],
    resume_state: str | None = None,
    clear_resume: bool = False,
    prepared_command_id: str | None = None,
) -> str | None:
    previous = conn.execute(
        text("SELECT status FROM tasks WHERE project_id = :project_id AND id = :id"),
        {"project_id": project_id, "id": task_id},
    ).scalar()
    names = [f"allowed_{index}" for index in range(len(allowed))]
    clause = ", ".join(f":{name}" for name in names)
    extra = ""
    params: dict[str, object] = {"status": status, "project_id": project_id, "id": task_id}
    params.update(dict(zip(names, allowed, strict=True)))
    if clear_resume:
        extra = ", resume_state = NULL"
    elif resume_state is not None:
        extra = ", resume_state = :resume_state"
        params["resume_state"] = resume_state
    updated = conn.execute(
        text(
            "UPDATE tasks SET status = :status, version = version + 1, projection_updated_at = now() "
            f"{extra} WHERE project_id = :project_id AND id = :id AND status IN ({clause}) RETURNING status"
        ),
        params,
    ).scalar()
    if updated is None:
        return None
    if str(updated) in STOP_STATUSES:
        conn.execute(
            text(
                "UPDATE capability_grants SET revoked_at = now() "
                "WHERE project_id = :project_id AND task_id = :task_id AND capability_type = 'model' AND revoked_at IS NULL"
            ),
            {"project_id": project_id, "task_id": task_id},
        )
    new_status = str(updated)
    previous_status = str(previous or "")
    _insert_task_event(
        conn,
        project_id=project_id,
        task_id=task_id,
        event_key=f"status-{task_id}-{new_status}-{uuid.uuid4()}",
        event_type="task.status",
        payload={"status": new_status},
    )
    if previous_status not in ACTIVE_STATUSES and new_status in ACTIVE_STATUSES:
        _insert_task_event(
            conn,
            project_id=project_id,
            task_id=task_id,
            event_key=f"active-start-{task_id}-{uuid.uuid4()}",
            event_type="task.active",
            payload={"phase": "start", "status": new_status},
        )
    elif previous_status in ACTIVE_STATUSES and new_status not in ACTIVE_STATUSES:
        _insert_task_event(
            conn,
            project_id=project_id,
            task_id=task_id,
            event_key=f"active-stop-{task_id}-{uuid.uuid4()}",
            event_type="task.active",
            payload={"phase": "stop", "status": new_status},
        )
    if prepared_command_id:
        _insert_task_event(
            conn,
            project_id=project_id,
            task_id=task_id,
            event_key=f"prepared-{prepared_command_id}",
            event_type="task.prepared",
            payload={"command_id": prepared_command_id},
        )
    return new_status


def _event(database: Database, *, owner_id: str, project_id: str, task_id: str, event_key: str, event_type: str, payload: dict[str, Any]) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text("SELECT 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_key = :event_key"),
            {"project_id": project_id, "task_id": task_id, "event_key": event_key},
        ).first()
        if existing is not None:
            return
        _insert_task_event(conn, project_id=project_id, task_id=task_id, event_key=event_key, event_type=event_type, payload=payload)


def _insert_task_event(conn: Any, *, project_id: str, task_id: str, event_key: str, event_type: str, payload: dict[str, Any]) -> None:
    """Allocate the next sequence while the task row is locked, and retry a unique collision."""
    conn.execute(
        text("SELECT id FROM tasks WHERE project_id = :project_id AND id = :task_id FOR UPDATE"),
        {"project_id": project_id, "task_id": task_id},
    )
    for _attempt in range(5):
        sequence = conn.execute(
            text("SELECT coalesce(max(sequence), 0) + 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id"),
            {"project_id": project_id, "task_id": task_id},
        ).scalar_one()
        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO task_events (project_id, task_id, sequence, event_key, event_type, payload) "
                        "VALUES (:project_id, :task_id, :sequence, :event_key, :event_type, CAST(:payload AS jsonb))"
                    ),
                    {
                        "project_id": project_id,
                        "task_id": task_id,
                        "sequence": sequence,
                        "event_key": event_key,
                        "event_type": event_type,
                        "payload": json.dumps(payload),
                    },
                )
            return
        except IntegrityError:
            continue
    raise RuntimeError("event sequence conflict")


def _review_artifact_error(endpoints: WorkerEndpoints, payload: dict[str, Any]) -> dict[str, str]:
    """A deterministic artifact rejection ends the task. It is not a reason to retry the activity."""
    command = _execution_context(endpoints, payload)
    task_id = str(payload.get("task_id") or "")
    if command is None:
        return {"status": "ignored", "task_id": task_id}
    owner_id, project_id, task_id, _goal = command
    worker = Database(endpoints.worker_url)
    return _needs_review(
        worker,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        reason="产物无法写入",
        code="artifact_rejected",
    )
