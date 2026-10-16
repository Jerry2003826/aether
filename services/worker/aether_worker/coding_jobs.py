"""Run queued coding jobs to a terminal state. Acceptance is not a passing grade."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, NoResultFound

from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.budget import BudgetError, BudgetLedger
from aether_storage.db import Database
from aether_worker.coding_sandbox import (
    LOG_TAIL_LIMIT,
    CodingSandboxError,
    confirm_coding_stop,
    container_name,
    run_coding_sandbox,
)
from aether_worker.coding_workspace import WorkspaceError, parse_workspace, unified_patch
from aether_worker.runner import JOB_IMAGE

_OPEN = ("QUEUED", "STARTING", "ACTIVE", "EXPORTING")
LEASE_SECONDS = 30
_UNCONFIRMED = frozenset({"cancel_unconfirmed", "deadline_unconfirmed", "timeout_unconfirmed"})
_CONFIRMED_STOP = frozenset({"cancelled", "deadline_exceeded", "timeout"})
_DEFAULT_BUDGET = {
    "max_cost_micro_usd": 2_000_000,
    "max_tokens": 100_000,
    "max_requests": 48,
    "verification_reserve_fraction": 0.2,
    "active_seconds": 1800,
}


class CodingError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def lease_blocks_reap(*, deadline: datetime, lease_until: datetime | None, now: datetime) -> bool:
    """A future deadline or a live lease means the reaper must leave the job alone."""
    if deadline > now:
        return True
    return lease_until is not None and lease_until > now


def terminal_state_after_stop(*, confirmed: bool, cancel: bool) -> str | None:
    """Unconfirmed stops stay non-terminal. Cancel is not a failure and not a rollback."""
    if not confirmed:
        return None
    if cancel:
        return "CANCELLED"
    return "FAILED"


def execute_queued_coding_jobs(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
) -> list[dict[str, str]]:
    reap_expired_coding_jobs(database, store, owner_id=owner_id, project_id=project_id)
    outcomes: list[dict[str, str]] = []
    for job_id in _open_ids(database, owner_id=owner_id, project_id=project_id):
        outcomes.append(_run_one(database, store, owner_id=owner_id, project_id=project_id, job_id=job_id))
    return outcomes


def reap_expired_coding_jobs(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
) -> list[dict[str, str]]:
    """Stop a job only after its deadline and only when no live lease still holds it."""
    outcomes: list[dict[str, str]] = []
    for row in _expired_rows(database, owner_id=owner_id, project_id=project_id):
        job_id = str(row["id"])
        if not _take_expired_lease(database, owner_id=owner_id, project_id=project_id, job_id=job_id, operation_id=str(row["operation_id"])):
            continue
        outcome = _stop_confirmed(
            database,
            store,
            owner_id=owner_id,
            project_id=project_id,
            row=row,
            reserved=False,
            ledger=BudgetLedger(database),
            request_key=f"coding-{job_id}-{int(row['fencing_token'])}",
            logs=None,
            root=None,
        )
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def _open_ids(database: Database, *, owner_id: str, project_id: str) -> list[str]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT j.id::text AS id FROM coding_jobs j "
                "JOIN operations o ON o.project_id = j.project_id AND o.id = j.operation_id "
                "WHERE j.project_id = :project_id AND j.state = ANY(:states) "
                "AND (o.lease_until IS NULL OR o.lease_until <= now()) "
                "ORDER BY j.created_at, j.id"
            ),
            {"project_id": project_id, "states": list(_OPEN)},
        ).scalars().all()
    return [str(item) for item in rows]


def _run_one(database: Database, store: ArtifactStore, *, owner_id: str, project_id: str, job_id: str) -> dict[str, str]:
    row = _claim(database, owner_id=owner_id, project_id=project_id, job_id=job_id)
    if row is None:
        return {"coding_job_id": job_id, "state": "SKIPPED"}
    fencing = int(row["fencing_token"])
    request_key = f"coding-{job_id}-{fencing}"
    reserved = False
    ledger = BudgetLedger(database)
    task_id = str(row["task_id"])
    try:
        early = _stop_confirmed(
            database,
            store,
            owner_id=owner_id,
            project_id=project_id,
            row=row,
            reserved=False,
            ledger=ledger,
            request_key=request_key,
            logs=None,
            root=None,
            only_if_due=True,
        )
        if early is not None:
            return early
        workspace = _workspace(store, owner_id=owner_id, project_id=project_id, artifact_id=str(row["base_artifact_id"]))
        _ensure_account(database, owner_id=owner_id, project_id=project_id, task_id=task_id)
        if not ledger.reserve(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            request_key=request_key,
            amount=1,
            role="coding",
            tokens=1,
        ):
            raise CodingError("budget_exceeded")
        reserved = True
        _mark(database, owner_id=owner_id, project_id=project_id, job_id=job_id, state="ACTIVE")
        _operation(database, owner_id=owner_id, project_id=project_id, operation_id=str(row["operation_id"]), state="DISPATCHED", error_code=None)
        with tempfile.TemporaryDirectory(prefix="aether-code-") as temporary:
            root = Path(temporary)
            _materialize(workspace, root)
            try:
                sandbox = run_coding_sandbox(
                    root,
                    job_id=job_id,
                    stop_requested=lambda: _cancel_requested(database, owner_id=owner_id, project_id=project_id, task_id=task_id),
                    deadline_reached=lambda: _deadline_passed(database, owner_id=owner_id, project_id=project_id, job_id=job_id),
                    on_poll=lambda: _renew_lease(database, owner_id=owner_id, project_id=project_id, operation_id=str(row["operation_id"]), job_id=job_id),
                )
            except CodingSandboxError as exc:
                stopped = _sandbox_stop(
                    database,
                    store,
                    owner_id=owner_id,
                    project_id=project_id,
                    row=row,
                    reserved=reserved,
                    ledger=ledger,
                    request_key=request_key,
                    root=root,
                    exc=exc,
                )
                if stopped is not None:
                    return stopped
                raise
            _settle(ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=True)
            reserved = False
            result = sandbox["result"]
            passed = int(result.get("passed") or 0) > 0 and int(result.get("failed") or 0) == 0 and sandbox["exit_code"] == 0
            error = None if passed else "test_failed"
            if result.get("credential_present"):
                passed = False
                error = "credential_present"
            if result.get("network_errno") == 0:
                passed = False
                error = "network_open"
            state = "FINISHED" if passed else "FAILED"
            _publish(
                database,
                store,
                owner_id=owner_id,
                project_id=project_id,
                row=row,
                workspace=workspace,
                sandbox=sandbox,
                state=state,
                error_code=error,
                manifest=_manifest(root, workspace),
            )
        return {"coding_job_id": job_id, "state": state}
    except (CodingError, CodingSandboxError, WorkspaceError, ArtifactError, BudgetError, OSError) as exc:
        code = exc.code if isinstance(exc, (CodingError, CodingSandboxError, WorkspaceError, ArtifactError)) else "coding_failed"
        if isinstance(exc, BudgetError):
            code = exc.code
        _release_hold(ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=reserved)
        _clear_lease(database, owner_id=owner_id, project_id=project_id, operation_id=str(row["operation_id"]))
        _fail(database, store, owner_id=owner_id, project_id=project_id, row=row, error_code=code)
        return {"coding_job_id": job_id, "state": "FAILED", "error_code": code}


def _claim(database: Database, *, owner_id: str, project_id: str, job_id: str) -> dict[str, Any] | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT id::text AS id, task_id::text AS task_id, operation_id::text AS operation_id, "
                "base_artifact_id::text AS base_artifact_id, fencing_token, deadline, state "
                "FROM coding_jobs WHERE project_id = :project_id AND id = :id AND state = ANY(:states) FOR UPDATE"
            ),
            {"project_id": project_id, "id": job_id, "states": list(_OPEN)},
        ).mappings().first()
        if row is None:
            return None
        taken = conn.execute(
            text(
                "UPDATE operations SET lease_until = now() + make_interval(secs => :lease_seconds) "
                "WHERE project_id = :project_id AND id = :operation_id AND (lease_until IS NULL OR lease_until <= now())"
            ),
            {"lease_seconds": LEASE_SECONDS, "project_id": project_id, "operation_id": row["operation_id"]},
        ).rowcount
        if taken != 1:
            return None
        conn.execute(
            text(
                "UPDATE coding_jobs SET state = 'STARTING', last_heartbeat_at = now(), container_reference = :name "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"name": container_name(job_id), "project_id": project_id, "id": job_id},
        )
    return dict(row)


def _sandbox_stop(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    row: dict[str, Any],
    reserved: bool,
    ledger: BudgetLedger,
    request_key: str,
    root: Path,
    exc: CodingSandboxError,
) -> dict[str, str] | None:
    if exc.code in _UNCONFIRMED:
        _release_hold(ledger, owner_id=owner_id, project_id=project_id, task_id=str(row["task_id"]), request_key=request_key, reserved=reserved)
        _clear_lease(database, owner_id=owner_id, project_id=project_id, operation_id=str(row["operation_id"]))
        return {"coding_job_id": str(row["id"]), "state": "ACTIVE", "error_code": exc.code}
    if exc.code not in _CONFIRMED_STOP:
        return None
    return _stop_confirmed(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        row=row,
        reserved=reserved,
        ledger=ledger,
        request_key=request_key,
        logs=exc.payload,
        root=root,
        already_confirmed=True,
        error_code=exc.code if exc.code == "timeout" else None,
    )


def _stop_confirmed(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    row: dict[str, Any],
    reserved: bool,
    ledger: BudgetLedger,
    request_key: str,
    logs: dict[str, Any] | None,
    root: Path | None,
    only_if_due: bool = False,
    already_confirmed: bool = False,
    error_code: str | None = None,
) -> dict[str, str] | None:
    job_id = str(row["id"])
    task_id = str(row["task_id"])
    cancel = _cancel_requested(database, owner_id=owner_id, project_id=project_id, task_id=task_id)
    expired = _deadline_passed(database, owner_id=owner_id, project_id=project_id, job_id=job_id)
    if only_if_due and not cancel and not expired:
        return None
    confirmed = True if already_confirmed else confirm_coding_stop(job_id)
    state = terminal_state_after_stop(confirmed=confirmed, cancel=cancel)
    if state is None:
        _release_hold(ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=reserved)
        _clear_lease(database, owner_id=owner_id, project_id=project_id, operation_id=str(row["operation_id"]))
        pending = "cancel_unconfirmed" if cancel else "deadline_unconfirmed"
        return {"coding_job_id": job_id, "state": "ACTIVE", "error_code": pending}
    workspace = _workspace(store, owner_id=owner_id, project_id=project_id, artifact_id=str(row["base_artifact_id"]))
    _settle(ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=reserved)
    chosen = error_code or ("cancelled" if state == "CANCELLED" else "deadline_exceeded")
    _publish_stop(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        row=row,
        workspace=workspace,
        state=state,
        error_code=chosen,
        manifest=_manifest(root, workspace),
        logs=logs or {},
        cancel=state == "CANCELLED",
    )
    return {"coding_job_id": job_id, "state": state, "error_code": chosen}


def _expired_rows(database: Database, *, owner_id: str, project_id: str) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT j.id::text AS id, j.task_id::text AS task_id, j.operation_id::text AS operation_id, "
                "j.base_artifact_id::text AS base_artifact_id, j.fencing_token, j.deadline, j.state "
                "FROM coding_jobs j WHERE j.project_id = :project_id AND j.state = ANY(:states) AND j.deadline <= now() "
                "ORDER BY j.created_at, j.id"
            ),
            {"project_id": project_id, "states": list(_OPEN)},
        ).mappings().all()
    return [dict(row) for row in rows]


def _take_expired_lease(database: Database, *, owner_id: str, project_id: str, job_id: str, operation_id: str) -> bool:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        taken = conn.execute(
            text(
                "UPDATE operations AS o SET lease_until = now() + make_interval(secs => :lease_seconds) "
                "FROM coding_jobs AS j "
                "WHERE o.project_id = :project_id AND o.id = :operation_id "
                "AND j.project_id = o.project_id AND j.id = :job_id AND j.operation_id = o.id "
                "AND j.state = ANY(:states) AND j.deadline <= now() "
                "AND (o.lease_until IS NULL OR o.lease_until < now())"
            ),
            {
                "lease_seconds": LEASE_SECONDS,
                "project_id": project_id,
                "operation_id": operation_id,
                "job_id": job_id,
                "states": list(_OPEN),
            },
        ).rowcount
    return taken == 1


def _renew_lease(database: Database, *, owner_id: str, project_id: str, operation_id: str, job_id: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE operations SET lease_until = now() + make_interval(secs => :lease_seconds) "
                "WHERE project_id = :project_id AND id = :operation_id AND lease_until > now()"
            ),
            {"lease_seconds": LEASE_SECONDS, "project_id": project_id, "operation_id": operation_id},
        )
        conn.execute(
            text(
                "UPDATE coding_jobs SET last_heartbeat_at = now() "
                "WHERE project_id = :project_id AND id = :id AND state = ANY(:states)"
            ),
            {"project_id": project_id, "id": job_id, "states": list(_OPEN)},
        )


def _clear_lease(database: Database, *, owner_id: str, project_id: str, operation_id: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE operations SET lease_until = NULL WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": operation_id},
        )


def _cancel_requested(database: Database, *, owner_id: str, project_id: str, task_id: str) -> bool:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text("SELECT resume_state, status FROM tasks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": task_id},
        ).mappings().first()
    if row is None:
        return False
    return row["resume_state"] == "cancel_requested" or row["status"] == "CANCELLED"


def _deadline_passed(database: Database, *, owner_id: str, project_id: str, job_id: str) -> bool:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        deadline = conn.execute(
            text("SELECT deadline FROM coding_jobs WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": job_id},
        ).scalar()
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline <= datetime.now(UTC)


def _manifest(root: Path | None, workspace: dict[str, dict[str, str]]) -> list[str]:
    names: list[str] = []
    if root is not None and root.exists():
        names = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    if names:
        return names
    return sorted({*workspace["files"], *workspace["tests"]})


def _workspace(store: ArtifactStore, *, owner_id: str, project_id: str, artifact_id: str) -> dict[str, dict[str, str]]:
    data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
    return parse_workspace(data)


def _ensure_account(database: Database, *, owner_id: str, project_id: str, task_id: str) -> None:
    spec = _budget_spec(database, owner_id=owner_id, project_id=project_id, task_id=task_id)
    try:
        BudgetLedger(database).open_account(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            limit_micro_usd=int(spec["max_cost_micro_usd"]),
            token_limit=int(spec["max_tokens"]),
            max_requests=int(spec["max_requests"]),
            verification_reserve_fraction=float(spec["verification_reserve_fraction"]),
            active_seconds=int(spec["active_seconds"]),
        )
    except IntegrityError:
        return


def _budget_spec(database: Database, *, owner_id: str, project_id: str, task_id: str) -> dict[str, Any]:
    spec = dict(_DEFAULT_BUDGET)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        payload = conn.execute(
            text(
                "SELECT payload FROM command_outbox WHERE project_id = :project_id AND task_id = :task_id AND kind = 'start' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).scalar()
    body = payload if isinstance(payload, dict) else json.loads(payload) if isinstance(payload, str) else {}
    budget = body.get("budget") if isinstance(body, dict) else None
    if isinstance(budget, dict):
        for key in spec:
            if key in budget and not isinstance(budget[key], bool):
                spec[key] = budget[key]
    return spec


def _materialize(workspace: dict[str, dict[str, str]], root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    for name, content in {**workspace["files"], **workspace["tests"]}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o644)


def _publish(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    row: dict[str, Any],
    workspace: dict[str, dict[str, str]],
    sandbox: dict[str, Any],
    state: str,
    error_code: str | None,
    manifest: list[str],
) -> None:
    _mark(database, owner_id=owner_id, project_id=project_id, job_id=str(row["id"]), state="EXPORTING")
    patch = unified_patch(workspace["base_files"], workspace["files"]).encode()
    result = json.dumps({"kind": "aether.coding_test_result", **sandbox["result"]}, ensure_ascii=False, separators=(",", ":")).encode()
    events = _events(sandbox, error_code, manifest=manifest, patch=patch)
    _store_terminal(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        row=row,
        state=state,
        error_code=error_code,
        patch=patch,
        result=result,
        events=events,
        revoke=False,
    )


def _publish_stop(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    row: dict[str, Any],
    workspace: dict[str, dict[str, str]],
    state: str,
    error_code: str,
    manifest: list[str],
    logs: dict[str, Any],
    cancel: bool,
) -> None:
    _mark(database, owner_id=owner_id, project_id=project_id, job_id=str(row["id"]), state="EXPORTING")
    patch = unified_patch(workspace["base_files"], workspace["files"]).encode()
    stdout, stderr = _tails(logs)
    result = json.dumps(
        {"kind": "aether.coding_test_result", "passed": 0, "failed": 0, "errors": 1, "cases": [], "error_code": error_code},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    events = _stop_events(error_code=error_code, manifest=manifest, patch=patch, stdout=stdout, stderr=stderr, cancel=cancel)
    _store_terminal(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        row=row,
        state=state,
        error_code=error_code,
        patch=patch,
        result=result,
        events=events,
        revoke=True,
    )


def _fail(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    row: dict[str, Any],
    error_code: str,
) -> None:
    events = json.dumps(
        {"kind": "aether.coding_events", "error_code": error_code, "network": "none", "read_only": True, "image": JOB_IMAGE},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    result = json.dumps(
        {"kind": "aether.coding_test_result", "passed": 0, "failed": 0, "errors": 1, "cases": [], "error_code": error_code},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    try:
        _store_terminal(
            database,
            store,
            owner_id=owner_id,
            project_id=project_id,
            row=row,
            state="FAILED",
            error_code=error_code,
            patch=b"--- a/empty\n+++ b/empty\n",
            result=result,
            events=events,
            revoke=False,
        )
    except (ArtifactError, OSError):
        _mark(database, owner_id=owner_id, project_id=project_id, job_id=str(row["id"]), state="FAILED")
        _operation(database, owner_id=owner_id, project_id=project_id, operation_id=str(row["operation_id"]), state="FAILED", error_code=error_code)


def _events(sandbox: dict[str, Any], error_code: str | None, *, manifest: list[str], patch: bytes) -> bytes:
    stdout, stderr = _tails(sandbox)
    body = {
        "kind": "aether.coding_events",
        "exit_code": sandbox["exit_code"],
        "image": JOB_IMAGE,
        "network": "none",
        "read_only": True,
        "pids_limit": 64,
        "error_code": error_code,
        "network_errno": sandbox["result"].get("network_errno"),
        "credential_present": sandbox["result"].get("credential_present"),
        "file_manifest": manifest,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "stdout_tail": stdout,
        "stderr_tail": stderr,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def _stop_events(*, error_code: str, manifest: list[str], patch: bytes, stdout: str, stderr: str, cancel: bool) -> bytes:
    body: dict[str, Any] = {
        "kind": "aether.coding_events",
        "image": JOB_IMAGE,
        "network": "none",
        "read_only": True,
        "pids_limit": 64,
        "error_code": error_code,
        "external_effect": "unknown",
        "file_manifest": manifest,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "stdout_tail": stdout,
        "stderr_tail": stderr,
    }
    if cancel:
        body["cancel_state"] = "confirmed"
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def _tails(payload: dict[str, Any]) -> tuple[str, str]:
    stdout = str(payload.get("stdout_tail") or payload.get("stdout") or "")
    stderr = str(payload.get("stderr_tail") or payload.get("stderr") or "")
    return stdout[-LOG_TAIL_LIMIT:], stderr[-LOG_TAIL_LIMIT:]


def _store_terminal(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    row: dict[str, Any],
    state: str,
    error_code: str | None,
    patch: bytes,
    result: bytes,
    events: bytes,
    revoke: bool,
) -> None:
    task_id = str(row["task_id"])
    job_id = str(row["id"])
    patch_id = str(store.put(owner_id=owner_id, project_id=project_id, data=patch, media_type="text/plain", classification="coding_patch")["artifact_id"])
    result_id = str(store.put(owner_id=owner_id, project_id=project_id, data=result, media_type="application/json", classification="coding_test_result")["artifact_id"])
    events_id = str(store.put(owner_id=owner_id, project_id=project_id, data=events, media_type="application/json", classification="coding_events")["artifact_id"])
    operation_state = "CONFIRMED" if state == "FINISHED" else "FAILED"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        for artifact_id, classification in (
            (patch_id, "coding_patch"),
            (result_id, "coding_test_result"),
            (events_id, "coding_events"),
        ):
            _link(conn, project_id=project_id, task_id=task_id, artifact_id=artifact_id, classification=classification)
        updated = conn.execute(
            text(
                "UPDATE coding_jobs SET state = :state, patch_artifact_id = :patch, result_artifact_id = :result, "
                "events_artifact_id = :events, last_heartbeat_at = now() "
                "WHERE project_id = :project_id AND id = :id AND state = ANY(:open_states)"
            ),
            {
                "state": state,
                "patch": patch_id,
                "result": result_id,
                "events": events_id,
                "project_id": project_id,
                "id": job_id,
                "open_states": list(_OPEN),
            },
        ).rowcount
        if updated != 1:
            return
        conn.execute(
            text(
                "UPDATE operations SET state = :state, error_code = :error, resolved_at = now(), result = CAST(:result AS jsonb) "
                "WHERE project_id = :project_id AND id = :id AND state IN ('PREPARED', 'DISPATCHED')"
            ),
            {
                "state": operation_state,
                "error": error_code,
                "result": json.dumps({"patch_artifact_id": patch_id, "result_artifact_id": result_id, "events_artifact_id": events_id}),
                "project_id": project_id,
                "id": row["operation_id"],
            },
        )
        if revoke:
            conn.execute(
                text(
                    "UPDATE capability_grants AS g SET revoked_at = COALESCE(g.revoked_at, now()) "
                    "FROM operations AS o "
                    "WHERE o.project_id = :project_id AND o.id = :operation_id "
                    "AND g.project_id = o.project_id AND g.id = o.capability_id"
                ),
                {"project_id": project_id, "operation_id": row["operation_id"]},
            )


def _link(conn: Any, *, project_id: str, task_id: str, artifact_id: str, classification: str) -> None:
    found = conn.execute(
        text(
            "SELECT 1 FROM task_artifacts WHERE project_id = :project_id AND task_id = :task_id "
            "AND artifact_id = :artifact_id AND classification = :classification AND deleted_at IS NULL"
        ),
        {"project_id": project_id, "task_id": task_id, "artifact_id": artifact_id, "classification": classification},
    ).first()
    if found is not None:
        return
    conn.execute(
        text(
            "INSERT INTO task_artifacts (project_id, task_id, artifact_id, classification) "
            "VALUES (:project_id, :task_id, :artifact_id, :classification)"
        ),
        {"project_id": project_id, "task_id": task_id, "artifact_id": artifact_id, "classification": classification},
    )


def _mark(database: Database, *, owner_id: str, project_id: str, job_id: str, state: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE coding_jobs SET state = :state, last_heartbeat_at = now() "
                "WHERE project_id = :project_id AND id = :id AND state = ANY(:open_states)"
            ),
            {"state": state, "project_id": project_id, "id": job_id, "open_states": list(_OPEN)},
        )


def _operation(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    operation_id: str,
    state: str,
    error_code: str | None,
) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE operations SET state = :state, error_code = :error, "
                "dispatched_at = CASE WHEN :state = 'DISPATCHED' THEN now() ELSE dispatched_at END, "
                "resolved_at = CASE WHEN :state IN ('CONFIRMED', 'FAILED') THEN now() ELSE resolved_at END "
                "WHERE project_id = :project_id AND id = :id AND state IN ('PREPARED', 'DISPATCHED')"
            ),
            {"state": state, "error": error_code, "project_id": project_id, "id": operation_id},
        )


def _settle(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
    reserved: bool,
) -> None:
    if not reserved:
        return
    try:
        ledger.settle(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            request_key=request_key,
            actual=0,
            input_tokens=0,
            output_tokens=0,
        )
    except (BudgetError, NoResultFound):
        return


def _release_hold(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
    reserved: bool,
) -> None:
    if not reserved:
        return
    try:
        ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key)
    except BudgetError:
        return
