"""Run queued coding jobs to a terminal state. Acceptance is not a passing grade."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, NoResultFound

from aether_gateway.model import ModelGateway
from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.budget import BudgetError, BudgetLedger, coding_call_micro_usd, move_call_cost, priced_micro_usd
from aether_storage.db import Database
from aether_worker.coding_model_proxy import CodingModelProxy
from aether_worker.coding_sandbox import (
    LOG_TAIL_LIMIT,
    CodingSandboxError,
    confirm_coding_stop,
    container_name,
    run_coding_sandbox,
)
from aether_worker.coding_workspace import UNCHANGED_PATCH, WorkspaceError, parse_workspace, unified_patch
from aether_worker.grading_files import GRADING_MANIFEST
from aether_worker.runner import JOB_IMAGE

_OPEN = ("QUEUED", "STARTING", "ACTIVE", "EXPORTING")
LEASE_SECONDS = 30
_UNCONFIRMED = frozenset({"cancel_unconfirmed", "deadline_unconfirmed", "timeout_unconfirmed", "stale_container_unconfirmed"})
_CONFIRMED_STOP = frozenset({"cancelled", "deadline_exceeded", "timeout"})
CODING_CALL_RESERVE_TOKENS = 4096
CODING_CALL_RESERVE_MICRO_USD = 4096
CODING_RESERVE_TOKENS = CODING_CALL_RESERVE_TOKENS
CODING_RESERVE_MICRO_USD = CODING_CALL_RESERVE_MICRO_USD


def coding_call_reserve_tokens() -> int:
    return _positive_env("AETHER_CODING_CALL_RESERVE_TOKENS", CODING_CALL_RESERVE_TOKENS)


def coding_call_reserve_micro_usd() -> int:
    raw = os.environ.get("AETHER_CODING_CALL_RESERVE_MICRO_USD", "").strip()
    if not raw:
        return coding_call_reserve_tokens()
    return _positive_env("AETHER_CODING_CALL_RESERVE_MICRO_USD", coding_call_reserve_tokens())


def _positive_env(name: str, default: int) -> int:
    value = _positive_value(os.environ.get(name, ""))
    return default if value is None else value


def _positive_value(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 1:
        return None
    return value


_LOG = logging.getLogger("aether.worker.coding")


def warn_coding_rate_default() -> None:
    """Say once, at worker startup, when the coding money rate is not taken from the environment.

    Then the money per call defaults to the token count: 1 micro-USD per token, a bookkeeping unit
    and not a price. It stays a supported setup, so this only warns. A rejected value is not echoed.
    """
    raw = os.environ.get("AETHER_CODING_CALL_RESERVE_MICRO_USD", "")
    if _positive_value(raw) is not None:
        return
    reason = "unset" if not raw.strip() else "invalid"
    tokens = coding_call_reserve_tokens()
    _LOG.warning(
        "coding_rate_default AETHER_CODING_CALL_RESERVE_MICRO_USD=%s default=%s micro_usd_per_call "
        "(=AETHER_CODING_CALL_RESERVE_TOKENS=%s) rate=1 micro_usd_per_token accounting_unit_not_a_price",
        reason,
        tokens,
        tokens,
    )
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


class _LeaseLost(Exception):
    """The lease could not be renewed: another worker may own the job, so this one must not settle."""


# The proxy drain renews the lease this often, well inside LEASE_SECONDS.
DRAIN_RENEW_SECONDS = LEASE_SECONDS / 3


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
        taken = _take_expired_lease(
            database,
            owner_id=owner_id,
            project_id=project_id,
            job_id=job_id,
            operation_id=str(row["operation_id"]),
            fencing_token=int(row["fencing_token"]),
        )
        if taken is None:
            continue
        # Taking the job moved the fence: from here on the row is the reaper's attempt.
        row = {**row, "fencing_token": taken}
        ledger = BudgetLedger(database)
        try:
            _settle_prior_attempts(
                ledger, owner_id=owner_id, project_id=project_id, task_id=str(row["task_id"]), job_id=job_id, fencing=taken
            )
            outcome = _stop_confirmed(
                database,
                store,
                owner_id=owner_id,
                project_id=project_id,
                row=row,
                reserved=False,
                ledger=ledger,
                request_key=attempt_request_key(job_id, taken),
                logs=None,
                root=None,
            )
        except _LeaseLost:
            # Someone took the job again in the meantime; it is theirs to stop.
            continue
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
    request_key = attempt_request_key(job_id, fencing)
    fence = (job_id, fencing)
    reserved = False
    ledger = BudgetLedger(database)
    task_id = str(row["task_id"])
    try:
        # The claim moved the fence, so this attempt books on a row of its own; an earlier
        # attempt's reservation is closed here, not reused and not left held next to this one.
        _settle_prior_attempts(ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, job_id=job_id, fencing=fencing)
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
        objective = _job_objective(row)
        workspace = _workspace(store, owner_id=owner_id, project_id=project_id, artifact_id=str(row["base_artifact_id"]))
        _ensure_account(database, owner_id=owner_id, project_id=project_id, task_id=task_id)
        reserve_tokens = coding_call_reserve_tokens()
        reserve_micro = coding_call_reserve_micro_usd()
        if not ledger.reserve(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            request_key=request_key,
            amount=reserve_micro,
            role="coding",
            tokens=reserve_tokens,
            extra_detail={"call_reserve_tokens": reserve_tokens, "call_reserve_micro_usd": reserve_micro},
        ):
            raise CodingError("budget_exceeded")
        reserved = True
        _mark(
            database,
            owner_id=owner_id,
            project_id=project_id,
            job_id=job_id,
            state="ACTIVE",
            fencing_token=fencing,
            operation_id=str(row["operation_id"]),
        )
        _operation(
            database,
            owner_id=owner_id,
            project_id=project_id,
            operation_id=str(row["operation_id"]),
            state="DISPATCHED",
            error_code=None,
            job_id=job_id,
            fencing_token=fencing,
        )
        with tempfile.TemporaryDirectory(prefix="aether-code-") as temporary:
            root = Path(temporary)
            _materialize(workspace, root)
            model_token = _issue_coding_token(
                database,
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                request_key=request_key,
                attempt_id=str(fencing),
                job_id=job_id,
                operation_id=str(row["operation_id"]),
            )
            proxy: CodingModelProxy | None = None
            drained = False
            channel_revoked = False

            def keep_lease() -> None:
                # Only an explicit True is a renewed lease (never fail open): False means it expired or the fencing token moved on;
                # a database error propagates on its own.
                renewed = _renew_lease(
                    database,
                    owner_id=owner_id,
                    project_id=project_id,
                    operation_id=str(row["operation_id"]),
                    job_id=job_id,
                    fencing_token=fencing,
                )
                if renewed is not True:
                    raise _LeaseLost(job_id)

            def drain_once() -> None:
                # Settle and close only after calls still in flight finished; at most once per job.
                nonlocal drained
                if proxy is None or drained:
                    return
                drained = True
                # Host-side backstop: whatever the sandbox did, the channel is closed once its container
                # is done. Calls already in flight still answer and are settled by the drain below.
                proxy.revoke()
                _drain_proxy(
                    proxy,
                    ledger,
                    owner_id=owner_id,
                    project_id=project_id,
                    task_id=task_id,
                    request_key=request_key,
                    heartbeat=keep_lease,
                    fence=fence,
                )

            try:
                proxy = CodingModelProxy(database, job_token=model_token)
                keep_lease()
                try:
                    sandbox = run_coding_sandbox(
                        root,
                        objective=objective,
                        job_id=job_id,
                        proxy_dir=proxy.directory,
                        job_token=model_token,
                        stop_requested=lambda: _cancel_requested(database, owner_id=owner_id, project_id=project_id, task_id=task_id),
                        deadline_reached=lambda: _deadline_passed(database, owner_id=owner_id, project_id=project_id, job_id=job_id),
                        # A lost lease raises _LeaseLost out of the poll; the sandbox removes its container first.
                        on_poll=keep_lease,
                        # Reading the exit logs can take CLI_LOGS_TIMEOUT_SECONDS; keep the lease meanwhile.
                        heartbeat=keep_lease,
                    )
                except CodingSandboxError as exc:
                    drain_once()
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
                # Read once, here: run_coding_sandbox has returned (the container exited) and the
                # worker's own revoke in drain_once has not run yet, so this is whether the sandbox
                # entry revoked in band while its container ran. Not read on the CodingSandboxError path.
                channel_revoked = proxy.revoked_in_band
                # A call can still be forwarding after the agent exits; settle only once it is done.
                drain_once()
                _settle(
                    ledger,
                    owner_id=owner_id,
                    project_id=project_id,
                    task_id=task_id,
                    request_key=request_key,
                    reserved=True,
                    result=sandbox["result"],
                    fence=fence,
                )
                reserved = False
            finally:
                if proxy is not None:
                    try:
                        drain_once()
                    except _LeaseLost:
                        # The lease is gone: whatever got us here, this worker must not settle or fail
                        # the job, so the lost lease replaces the pending error and returns fence_lost.
                        _LOG.warning("coding_lease_lost_in_final_drain job=%s fencing=%s", job_id, fencing, exc_info=True)
                        raise
                    except Exception:  # noqa: BLE001 - keep the error that got us here
                        _LOG.exception("coding_proxy_drain_failed job=%s", job_id)
                    finally:
                        proxy.close()
            detail = _request_detail(
                ledger,
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                request_key=request_key,
            )
            measured = _measured_from_detail(detail)
            upstream_error, usage_missing = _relay_flags(detail)
            admission_error = detail.get("admission_error")
            blocked = admission_error if isinstance(admission_error, str) and admission_error else None
            state, error = _coding_terminal(
                sandbox,
                upstream_error=upstream_error,
                measured=measured,
                usage_missing=usage_missing,
                admission_error=blocked,
                channel_revoked=channel_revoked,
            )
            observed = _successful_model_requests(
                ledger,
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                request_key=request_key,
            )
            result = sandbox.get("result")
            if isinstance(result, dict):
                result["model_requests"] = observed
            _log_job(
                job_id=job_id,
                fencing=fencing,
                state=state,
                error=error,
                result=result if isinstance(result, dict) else {},
                measured=measured,
                patch_source=_patch_source(sandbox),
            )
            published = _publish(
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
        if not published:
            return {"coding_job_id": job_id, "state": "ACTIVE", "error_code": "fence_lost"}
        return {"coding_job_id": job_id, "state": state}
    except _LeaseLost:
        # Same as a fence lost at publish: the job is no longer ours, so nothing is settled,
        # released or cleared here; the worker that holds the lease now finishes it.
        _LOG.warning("coding_lease_lost_during_drain job=%s fencing=%s", job_id, fencing)
        return {"coding_job_id": job_id, "state": "ACTIVE", "error_code": "fence_lost"}
    except (CodingError, CodingSandboxError, WorkspaceError, ArtifactError, BudgetError, OSError) as exc:
        code = exc.code if isinstance(exc, (CodingError, CodingSandboxError, WorkspaceError, ArtifactError)) else "coding_failed"
        if isinstance(exc, BudgetError):
            code = exc.code
        _release_hold(
            ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=reserved, fence=fence
        )
        _clear_lease(
            database,
            owner_id=owner_id,
            project_id=project_id,
            operation_id=str(row["operation_id"]),
            job_id=job_id,
            fencing_token=fencing,
        )
        _fail(database, store, owner_id=owner_id, project_id=project_id, row=row, error_code=code)
        return {"coding_job_id": job_id, "state": "FAILED", "error_code": code}


def _claim(database: Database, *, owner_id: str, project_id: str, job_id: str) -> dict[str, Any] | None:
    try:
        with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT id::text AS id, task_id::text AS task_id, operation_id::text AS operation_id, "
                    "base_artifact_id::text AS base_artifact_id, fencing_token, deadline, state, "
                    "(SELECT o.normalized_arguments ->> 'objective' FROM operations o "
                    "WHERE o.project_id = coding_jobs.project_id AND o.id = coding_jobs.operation_id) AS objective "
                    "FROM coding_jobs WHERE project_id = :project_id AND id = :id AND state = ANY(:states) FOR UPDATE"
                ),
                {"project_id": project_id, "id": job_id, "states": list(_OPEN)},
            ).mappings().first()
            if row is None:
                return None
            # Every claim is a new holder, so it moves the fence: the worker whose lease lapsed may
            # still be alive, and must fail to renew, publish, settle or clear from now on.
            taken_fence = int(row["fencing_token"]) + 1
            taken = conn.execute(
                text(
                    "UPDATE operations SET lease_until = now() + make_interval(secs => :lease_seconds), fencing_token = :taken_fence "
                    "WHERE project_id = :project_id AND id = :operation_id AND (lease_until IS NULL OR lease_until <= now())"
                ),
                {"lease_seconds": LEASE_SECONDS, "taken_fence": taken_fence, "project_id": project_id, "operation_id": row["operation_id"]},
            ).rowcount
            if taken != 1:
                return None
            updated = conn.execute(
                text(
                    "UPDATE coding_jobs SET state = 'STARTING', fencing_token = :taken_fence, last_heartbeat_at = now(), "
                    "container_reference = :name "
                    "WHERE project_id = :project_id AND id = :id AND fencing_token = :fencing AND operation_id = :operation_id"
                ),
                {
                    "name": container_name(job_id),
                    "taken_fence": taken_fence,
                    "project_id": project_id,
                    "id": job_id,
                    "fencing": row["fencing_token"],
                    "operation_id": row["operation_id"],
                },
            ).rowcount
            if updated != 1:
                raise CodingError("fence_lost")
        claimed = dict(row)
        claimed["fencing_token"] = taken_fence
        return claimed
    except CodingError:
        return None


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
        _release_hold(
            ledger,
            owner_id=owner_id,
            project_id=project_id,
            task_id=str(row["task_id"]),
            request_key=request_key,
            reserved=reserved,
            fence=(str(row["id"]), int(row["fencing_token"])),
        )
        _clear_lease(
            database,
            owner_id=owner_id,
            project_id=project_id,
            operation_id=str(row["operation_id"]),
            job_id=str(row["id"]),
            fencing_token=int(row["fencing_token"]),
        )
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
    fence = (job_id, int(row["fencing_token"]))
    if state is None:
        _release_hold(
            ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=reserved, fence=fence
        )
        _clear_lease(
            database,
            owner_id=owner_id,
            project_id=project_id,
            operation_id=str(row["operation_id"]),
            job_id=job_id,
            fencing_token=int(row["fencing_token"]),
        )
        pending = "cancel_unconfirmed" if cancel else "deadline_unconfirmed"
        return {"coding_job_id": job_id, "state": "ACTIVE", "error_code": pending}
    workspace = _workspace(store, owner_id=owner_id, project_id=project_id, artifact_id=str(row["base_artifact_id"]))
    _settle(ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=reserved, fence=fence)
    chosen = error_code or ("cancelled" if state == "CANCELLED" else "deadline_exceeded")
    stored = _publish_stop(
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
    if not stored:
        return {"coding_job_id": job_id, "state": "ACTIVE", "error_code": "fence_lost"}
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


def _take_expired_lease(
    database: Database, *, owner_id: str, project_id: str, job_id: str, operation_id: str, fencing_token: int
) -> int | None:
    """Take an expired job's lapsed lease for the reaper; the new fencing token, or None if not taken.

    Like a claim, the take moves the fence, so a worker still alive on the old token loses the job.
    """
    taken_fence = fencing_token + 1
    try:
        with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            taken = conn.execute(
                text(
                    "UPDATE operations AS o SET lease_until = now() + make_interval(secs => :lease_seconds), fencing_token = :taken_fence "
                    "FROM coding_jobs AS j "
                    "WHERE o.project_id = :project_id AND o.id = :operation_id "
                    "AND j.project_id = o.project_id AND j.id = :job_id AND j.operation_id = o.id "
                    "AND j.fencing_token = :fencing AND j.state = ANY(:states) AND j.deadline <= now() "
                    "AND (o.lease_until IS NULL OR o.lease_until < now())"
                ),
                {
                    "lease_seconds": LEASE_SECONDS,
                    "taken_fence": taken_fence,
                    "project_id": project_id,
                    "operation_id": operation_id,
                    "job_id": job_id,
                    "fencing": fencing_token,
                    "states": list(_OPEN),
                },
            ).rowcount
            if taken != 1:
                return None
            moved = conn.execute(
                text(
                    "UPDATE coding_jobs SET fencing_token = :taken_fence "
                    "WHERE project_id = :project_id AND id = :id AND operation_id = :operation_id AND fencing_token = :fencing"
                ),
                {"taken_fence": taken_fence, "project_id": project_id, "id": job_id, "operation_id": operation_id, "fencing": fencing_token},
            ).rowcount
            if moved != 1:
                raise CodingError("fence_lost")  # rolls the lease back with it
    except CodingError:
        return None
    return taken_fence


def attempt_request_key(job_id: str, fencing: int) -> str:
    """The parent reservation of one attempt. Every claim or reaper take is a new attempt and key."""
    return f"coding-{job_id}-{fencing}"


def _fence_guard(project_id: str, job_id: str, fencing: int) -> Callable[[Any], bool]:
    """True while the job still carries this fencing token. Shares the row lock order with a claim."""

    def held(conn: Any) -> bool:
        return (
            conn.execute(
                text("SELECT 1 FROM coding_jobs WHERE project_id = :project_id AND id = :id AND fencing_token = :fencing FOR SHARE"),
                {"project_id": project_id, "id": job_id, "fencing": fencing},
            ).first()
            is not None
        )

    return held


def _settle_prior_attempts(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    job_id: str,
    fencing: int,
) -> None:
    """Close every earlier attempt's parent reservation that is still held, for the attempt that took the job.

    That attempt's proxy token carries the old fencing token, which the gateway now refuses, so the
    row can no longer grow. It is closed as if its own drain had given up on every call still in
    flight: pending tokens move to unknown_reserved_tokens and are charged at the reservation's
    rate, measured tokens at their amount, and a late answer is not booked on top (proxy_drained).
    A row that never admitted a call carried nothing to a provider and is released in full.
    Each step is fenced on this attempt's token: if the job moves on again, the next holder does it.
    """
    prefix = f"coding-{job_id}-"
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT request_key FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                "AND role = 'coding' AND usage_status = 'reserved' AND request_key LIKE :prefix ORDER BY request_key"
            ),
            {"project_id": project_id, "task_id": task_id, "prefix": prefix + "%"},
        ).mappings().all()
    for item in rows:
        key = str(item["request_key"])
        suffix = key[len(prefix) :]
        if not suffix.isdigit() or int(suffix) >= fencing:
            continue
        _close_prior_attempt(
            ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=key, fence=(job_id, fencing)
        )


def _close_prior_attempt(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
    fence: tuple[str, int],
) -> None:
    job_id, fencing = fence
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        if not _fence_guard(project_id, job_id, fencing)(conn):
            raise _LeaseLost(job_id)
        raw = conn.execute(
            text(
                "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
            ),
            {"project_id": project_id, "task_id": task_id, "request_key": request_key},
        ).scalar()
        if raw is None:
            return
        detail = dict(raw) if isinstance(raw, dict) else dict(json.loads(raw) or {})
        pending = detail.get("pending_tokens")
        pending = pending if type(pending) is int and pending > 0 else 0
        detail["proxy_drained"] = True
        detail["superseded_by_fencing"] = fencing
        if pending:
            detail["takeover_pending_tokens"] = pending
            detail["unknown_reserved_tokens"] = _count_detail(detail.get("unknown_reserved_tokens")) + pending
            detail["pending_tokens"] = 0
            move_call_cost(detail, "pending", "unknown_reserved")
        conn.execute(
            text(
                "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
            ),
            {"detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
        )
        calls = conn.execute(
            text(
                "SELECT count(*) FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                "AND request_key LIKE :prefix"
            ),
            {"project_id": project_id, "task_id": task_id, "prefix": request_key + ":call:%"},
        ).scalar()
    _LOG.warning("coding_prior_attempt_closed request=%s fencing=%s pending=%s calls=%s", request_key, fencing, pending, calls)
    silent = _measured_from_detail(detail) is None and _unknown_reserved_tokens(detail) == 0 and detail.get("usage_missing") is not True
    if silent and not int(calls or 0):
        # Every call opens its coding_call ticket before the provider is contacted; none means nothing was sent.
        try:
            ledger.cancel_unused(
                owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key,
                guard=_fence_guard(project_id, job_id, fencing),
            )
        except BudgetError as exc:
            if exc.code == "fence_lost":
                raise _LeaseLost(job_id) from exc
            raise
        return
    _settle(ledger, owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, reserved=True, fence=fence)


def _count_detail(value: object) -> int:
    return value if type(value) is int and value > 0 else 0


def _renew_lease(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    operation_id: str,
    job_id: str,
    fencing_token: int,
) -> bool:
    """Extend a lease this worker still holds. False when it had expired or the fencing token moved on."""
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        renewed = conn.execute(
            text(
                "UPDATE operations AS o SET lease_until = now() + make_interval(secs => :lease_seconds) "
                "FROM coding_jobs AS j "
                "WHERE o.project_id = :project_id AND o.id = :operation_id "
                "AND j.project_id = o.project_id AND j.id = :job_id AND j.operation_id = o.id "
                "AND j.fencing_token = :fencing AND o.lease_until > now()"
            ),
            {
                "lease_seconds": LEASE_SECONDS,
                "project_id": project_id,
                "operation_id": operation_id,
                "job_id": job_id,
                "fencing": fencing_token,
            },
        ).rowcount
        conn.execute(
            text(
                "UPDATE coding_jobs SET last_heartbeat_at = now() "
                "WHERE project_id = :project_id AND id = :id AND fencing_token = :fencing "
                "AND operation_id = :operation_id AND state = ANY(:states)"
            ),
            {
                "project_id": project_id,
                "id": job_id,
                "fencing": fencing_token,
                "operation_id": operation_id,
                "states": list(_OPEN),
            },
        )
    return renewed == 1


def _clear_lease(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    operation_id: str,
    job_id: str,
    fencing_token: int,
) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE operations AS o SET lease_until = NULL "
                "FROM coding_jobs AS j "
                "WHERE o.project_id = :project_id AND o.id = :id "
                "AND j.project_id = o.project_id AND j.id = :job_id AND j.operation_id = o.id "
                "AND j.fencing_token = :fencing"
            ),
            {"project_id": project_id, "id": operation_id, "job_id": job_id, "fencing": fencing_token},
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
        names = sorted(
            path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path.relative_to(root).as_posix() != GRADING_MANIFEST
        )
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
    # Every trusted file is grading, whatever its name (Codex r4308195942): the sandbox reads this from /work.
    manifest = root / GRADING_MANIFEST
    manifest.write_text(json.dumps(sorted(workspace["tests"])), encoding="utf-8")
    manifest.chmod(0o644)


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
) -> bool:
    _mark(
        database,
        owner_id=owner_id,
        project_id=project_id,
        job_id=str(row["id"]),
        state="EXPORTING",
        fencing_token=int(row["fencing_token"]),
        operation_id=str(row["operation_id"]),
    )
    patch, patch_error = _published_patch_or_error(workspace, sandbox, job_id=str(row["id"]))
    result = json.dumps({"kind": "aether.coding_test_result", **sandbox["result"]}, ensure_ascii=False, separators=(",", ":")).encode()
    events = _events(sandbox, error_code, manifest=manifest, patch=patch, patch_error=patch_error)
    return _store_terminal(
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
) -> bool:
    _mark(
        database,
        owner_id=owner_id,
        project_id=project_id,
        job_id=str(row["id"]),
        state="EXPORTING",
        fencing_token=int(row["fencing_token"]),
        operation_id=str(row["operation_id"]),
    )
    patch, patch_error = _candidate_patch(workspace, job_id=str(row["id"]))
    stdout, stderr = _tails(logs)
    result = json.dumps(
        {"kind": "aether.coding_test_result", "passed": 0, "failed": 0, "errors": 1, "cases": [], "error_code": error_code},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    events = _stop_events(
        error_code=error_code, manifest=manifest, patch=patch, stdout=stdout, stderr=stderr, cancel=cancel, patch_error=patch_error
    )
    return _store_terminal(
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
            patch=UNCHANGED_PATCH.encode(),
            result=result,
            events=events,
            revoke=False,
        )
    except (ArtifactError, OSError):
        _mark(
            database,
            owner_id=owner_id,
            project_id=project_id,
            job_id=str(row["id"]),
            state="FAILED",
            fencing_token=int(row["fencing_token"]),
            operation_id=str(row["operation_id"]),
        )
        _operation(
            database,
            owner_id=owner_id,
            project_id=project_id,
            operation_id=str(row["operation_id"]),
            state="FAILED",
            error_code=error_code,
            job_id=str(row["id"]),
            fencing_token=int(row["fencing_token"]),
        )


def _events(
    sandbox: dict[str, Any], error_code: str | None, *, manifest: list[str], patch: bytes, patch_error: str | None = None
) -> bytes:
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
        "executor": sandbox["result"].get("executor"),
        "input_tokens": sandbox["result"].get("input_tokens"),
        "output_tokens": sandbox["result"].get("output_tokens"),
        "edit_calls": sandbox["result"].get("edit_calls"),
        "model_requests": sandbox["result"].get("model_requests"),
        "patch_source": _patch_source(sandbox),
        "file_manifest": manifest,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "stdout_tail": stdout,
        "stderr_tail": stderr,
    }
    if patch_error:
        body["patch_error"] = patch_error
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def _stop_events(
    *, error_code: str, manifest: list[str], patch: bytes, stdout: str, stderr: str, cancel: bool, patch_error: str | None = None
) -> bytes:
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
    if patch_error:
        body["patch_error"] = patch_error
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
) -> bool:
    task_id = str(row["task_id"])
    job_id = str(row["id"])
    fencing = int(row["fencing_token"])
    operation_id = str(row["operation_id"])
    patch_id = str(store.put(owner_id=owner_id, project_id=project_id, data=patch, media_type="text/plain", classification="coding_patch")["artifact_id"])
    result_id = str(store.put(owner_id=owner_id, project_id=project_id, data=result, media_type="application/json", classification="coding_test_result")["artifact_id"])
    events_id = str(store.put(owner_id=owner_id, project_id=project_id, data=events, media_type="application/json", classification="coding_events")["artifact_id"])
    operation_state = "CONFIRMED" if state == "FINISHED" else "FAILED"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        updated = conn.execute(
            text(
                "UPDATE coding_jobs SET state = :state, patch_artifact_id = :patch, result_artifact_id = :result, "
                "events_artifact_id = :events, last_heartbeat_at = now() "
                "WHERE project_id = :project_id AND id = :id AND fencing_token = :fencing "
                "AND operation_id = :operation_id AND state = ANY(:open_states)"
            ),
            {
                "state": state,
                "patch": patch_id,
                "result": result_id,
                "events": events_id,
                "project_id": project_id,
                "id": job_id,
                "fencing": fencing,
                "operation_id": operation_id,
                "open_states": list(_OPEN),
            },
        ).rowcount
        if updated != 1:
            return False
        for artifact_id, classification in (
            (patch_id, "coding_patch"),
            (result_id, "coding_test_result"),
            (events_id, "coding_events"),
        ):
            _link(conn, project_id=project_id, task_id=task_id, artifact_id=artifact_id, classification=classification)
        operated = conn.execute(
            text(
                "UPDATE operations AS o SET state = :state, error_code = :error, resolved_at = now(), result = CAST(:result AS jsonb) "
                "FROM coding_jobs AS j "
                "WHERE o.project_id = :project_id AND o.id = :operation_id "
                "AND j.project_id = o.project_id AND j.id = :job_id AND j.operation_id = o.id "
                "AND j.fencing_token = :fencing AND o.state IN ('PREPARED', 'DISPATCHED')"
            ),
            {
                "state": operation_state,
                "error": error_code,
                "result": json.dumps({"patch_artifact_id": patch_id, "result_artifact_id": result_id, "events_artifact_id": events_id}),
                "project_id": project_id,
                "operation_id": operation_id,
                "job_id": job_id,
                "fencing": fencing,
            },
        ).rowcount
        if operated != 1:
            raise CodingError("fence_lost")
        if revoke:
            conn.execute(
                text(
                    "UPDATE capability_grants AS g SET revoked_at = COALESCE(g.revoked_at, now()) "
                    "FROM operations AS o "
                    "WHERE o.project_id = :project_id AND o.id = :operation_id "
                    "AND g.project_id = o.project_id AND g.id = o.capability_id"
                ),
                {"project_id": project_id, "operation_id": operation_id},
            )
        _append_coding_event(
            conn,
            project_id=project_id,
            task_id=task_id,
            job_id=job_id,
            fencing=fencing,
            state=state,
            error_code=error_code,
        )
    return True


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


def _append_coding_event(
    conn: Any,
    *,
    project_id: str,
    task_id: str,
    job_id: str,
    fencing: int,
    state: str,
    error_code: str | None,
) -> None:
    event_key = f"coding-job-{job_id}-{fencing}"
    existing = conn.execute(
        text("SELECT 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_key = :event_key"),
        {"project_id": project_id, "task_id": task_id, "event_key": event_key},
    ).first()
    if existing is not None:
        return
    conn.execute(
        text("SELECT id FROM tasks WHERE project_id = :project_id AND id = :task_id FOR UPDATE"),
        {"project_id": project_id, "task_id": task_id},
    )
    payload = json.dumps({"coding_job_id": job_id, "state": state, "error_code": error_code})
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
                        "VALUES (:project_id, :task_id, :sequence, :event_key, 'coding_job.terminal', CAST(:payload AS jsonb))"
                    ),
                    {
                        "project_id": project_id,
                        "task_id": task_id,
                        "sequence": sequence,
                        "event_key": event_key,
                        "payload": payload,
                    },
                )
                conn.execute(
                    text(
                        "UPDATE tasks SET projection_updated_at = now(), version = version + 1 "
                        "WHERE project_id = :project_id AND id = :id"
                    ),
                    {"project_id": project_id, "id": task_id},
                )
            return
        except IntegrityError:
            continue
    raise CodingError("event_conflict")


def _mark(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    job_id: str,
    state: str,
    fencing_token: int,
    operation_id: str,
) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE coding_jobs SET state = :state, last_heartbeat_at = now() "
                "WHERE project_id = :project_id AND id = :id AND fencing_token = :fencing "
                "AND operation_id = :operation_id AND state = ANY(:open_states)"
            ),
            {
                "state": state,
                "project_id": project_id,
                "id": job_id,
                "fencing": fencing_token,
                "operation_id": operation_id,
                "open_states": list(_OPEN),
            },
        )


def _operation(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    operation_id: str,
    state: str,
    error_code: str | None,
    job_id: str,
    fencing_token: int,
) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "UPDATE operations AS o SET state = :state, error_code = :error, "
                "dispatched_at = CASE WHEN :state = 'DISPATCHED' THEN now() ELSE dispatched_at END, "
                "resolved_at = CASE WHEN :state IN ('CONFIRMED', 'FAILED') THEN now() ELSE resolved_at END "
                "FROM coding_jobs AS j "
                "WHERE o.project_id = :project_id AND o.id = :operation_id "
                "AND j.project_id = o.project_id AND j.id = :job_id AND j.operation_id = o.id "
                "AND j.fencing_token = :fencing AND o.state IN ('PREPARED', 'DISPATCHED')"
            ),
            {
                "state": state,
                "error": error_code,
                "project_id": project_id,
                "operation_id": operation_id,
                "job_id": job_id,
                "fencing": fencing_token,
            },
        )


def _issue_coding_token(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
    attempt_id: str,
    job_id: str,
    operation_id: str,
) -> str:
    gateway = ModelGateway(database, upstream_base="", provider_key="")
    return gateway.issue_token(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
        attempt_id=attempt_id,
        capabilities=["coding:model"],
        aliases=["coder"],
        job_id=job_id,
        operation_id=operation_id,
    )


def _job_objective(row: dict[str, Any]) -> str:
    """The objective stored in the claimed operation's arguments. There is no default prompt."""
    objective = row.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise CodingError("objective_missing")
    return objective


# Case details the sandbox entry reports when the agent tampered with grading (exit code 1).
GRADING_VIOLATIONS = frozenset({"grading_startup_hook", "grading_shadow_module", "grading_unverified"})
# The protocol produced no result line (hung past its limit, killed, crashed). A graded failure with its
# own error code; never a violation, since nothing was shown to be forged.
GRADING_TIMEOUT = "grading_timeout"
# The model channel was not confirmed closed before grading (see ADR-F44). Our side failed to close
# it, nothing shows tampering, so it is its own failure code and never a violation.
MODEL_CHANNEL_OPEN = "model_channel_open"
# The entry omitted the agent's patch (patch_omitted=too_large) so the RESULT line stays whole.
PATCH_TOO_LARGE = "patch_too_large"
# The protocol's candidate files passed its copy cap: no test ran, so it is not a test failure.
CANDIDATE_FILES_TOO_LARGE = "candidate_files_too_large"


def _graded(body: dict[str, Any]) -> bool:
    """The result claims grading ran: a test case other than the runner's, or a passing count."""
    cases = body.get("cases") or []
    return int(body.get("passed") or 0) > 0 or any(isinstance(case, dict) and case.get("name") != "runner" for case in cases)


def _runner_details(sandbox: dict[str, Any]) -> set[str]:
    result = sandbox.get("result")
    if not isinstance(result, dict):
        return set()
    return {
        str(case["detail"])
        for case in result.get("cases") or []
        if isinstance(case, dict) and case.get("name") == "runner" and isinstance(case.get("detail"), str)
    }


def _suite_failure(sandbox: dict[str, Any]) -> str | None:
    """The detail of the protocol's whole-suite failure case (name "suite"), if that is the only case."""
    result = sandbox.get("result")
    cases = result.get("cases") if isinstance(result, dict) else None
    if not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], dict):
        return None
    case = cases[0]
    if case.get("name") != "suite" or case.get("verdict") != "FAIL" or not isinstance(case.get("detail"), str):
        return None
    return case["detail"]


def _grading_violation(sandbox: dict[str, Any]) -> str | None:
    result = sandbox.get("result")
    if not isinstance(result, dict) or sandbox.get("exit_code") != 1:
        return None
    for case in result.get("cases") or []:
        if isinstance(case, dict) and case.get("name") == "runner" and case.get("detail") in GRADING_VIOLATIONS:
            return str(case["detail"])
    return None


def _agent_patch(sandbox: dict[str, Any]) -> str | None:
    """The snapshot diff of the agent's workspace, however it was edited.

    edit_calls only counts file_editor calls; a change made through the terminal is in
    the diff too, so the count is logged but does not gate publication. A grading
    violation keeps the patch for review as well.
    """
    result = sandbox.get("result")
    if not isinstance(result, dict):
        return None
    patch = result.get("patch")
    if isinstance(patch, str) and (patch.startswith("--- ") or _git_patch_with_change(patch)):
        return patch
    return None


# Lines under a `diff --git` header that git writes only for an actual change: hunks, a binary
# patch with its data, file creation or deletion, a mode change, a rename or copy. A header with
# only an index line, or "Binary files ... differ" without --binary data, changes nothing.
_GIT_CHANGE_LINES = (
    "--- ",
    "+++ ",
    "@@ ",
    "GIT binary patch",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "rename from ",
    "copy from ",
)


def _git_patch_with_change(patch: str) -> bool:
    """A patch in git's own format (`diff --git a/... b/...`) that carries at least one change."""
    if not patch.startswith("diff --git "):
        return False
    return any(line.startswith(_GIT_CHANGE_LINES) for line in patch.splitlines()[1:])


def _patch_source(sandbox: dict[str, Any]) -> str:
    return "agent_edit" if _agent_patch(sandbox) is not None else "candidate_files"


def _published_patch_or_error(
    workspace: dict[str, dict[str, str]], sandbox: dict[str, Any], *, job_id: str
) -> tuple[bytes, str | None]:
    """The agent's patch, else the candidate patch; see _candidate_patch for when git cannot write it."""
    patch = _agent_patch(sandbox)
    if patch is not None:
        return patch.encode(), None
    return _candidate_patch(workspace, job_id=job_id)


def _candidate_patch(workspace: dict[str, dict[str, str]], *, job_id: str) -> tuple[bytes, str | None]:
    """The candidate files' patch, or the unchanged marker and the reason when git cannot write it.

    A patch that cannot be produced is no reason to change how the job ended: the caller keeps its
    own state and error code, and the events record patch_error next to them.
    """
    try:
        return unified_patch(workspace["base_files"], workspace["files"]).encode(), None
    except WorkspaceError as exc:
        _LOG.warning("coding_patch_unavailable job=%s error=%s", job_id, exc.code)
        return UNCHANGED_PATCH.encode(), exc.code


def _agent_completed(body: dict[str, Any]) -> bool:
    """Only an explicit true from the entry means the conversation reached FINISHED.

    The entry always writes the flag; a missing or non-boolean value is treated as interrupted,
    not inferred from the request count.
    """
    return body.get("agent_completed") is True


def _coding_terminal(
    sandbox: dict[str, Any],
    *,
    upstream_error: str | None,
    measured: tuple[int, int] | None,
    usage_missing: bool,
    channel_revoked: bool,
    admission_error: str | None = None,
) -> tuple[str, str | None]:
    """FINISHED needs a completed agent, an agent change and a passing protocol.

    A budget rejection or a blank report (the agent raised before it finished)
    is FAILED even when the files on disk already pass. A conversation that
    finishes without changing any source file (empty or missing patch; grading
    files and caches are never in it) is FAILED/agent_no_change: candidate files that
    already pass do not make a job FINISHED. edit_calls is not consulted; a
    terminal-only change shows up in the patch. channel_revoked (required, so
    no caller can skip the check by forgetting it) is proxy.revoked_in_band as read by
    _run_one after the container exited and before the worker's own revoke: whether the
    sandbox entry revoked the model channel in band. Graded results without it are
    FAILED/model_channel_open.
    """
    result = sandbox.get("result")
    body = result if isinstance(result, dict) else {}
    passed = int(body.get("passed") or 0) > 0 and int(body.get("failed") or 0) == 0 and sandbox.get("exit_code") == 0
    if body.get("credential_present"):
        return "FAILED", "credential_present"
    if body.get("network_errno") == 0:
        return "FAILED", "network_open"
    # The sandbox could not confirm the model channel closed and skipped grading; or (channel_revoked
    # false: no in-band revoke was recorded by the time the container exited) it reports grading
    # that may have run with the channel open.
    if MODEL_CHANNEL_OPEN in _runner_details(sandbox) or (not channel_revoked and _graded(body)):
        return "FAILED", MODEL_CHANNEL_OPEN
    violation = _grading_violation(sandbox)
    if violation:
        return "FAILED", violation
    if body.get("patch_omitted") == "too_large":
        # The entry dropped a patch too large for the RESULT line; there is no change to publish.
        return "FAILED", PATCH_TOO_LARGE
    if admission_error:
        return "FAILED", admission_error
    if not _agent_completed(body):
        return "FAILED", "agent_interrupted"
    if upstream_error:
        return "FAILED", upstream_error
    if usage_missing and measured is None:
        return "FAILED", "usage_missing"
    requests = body.get("model_requests")
    called = isinstance(requests, int) and not isinstance(requests, bool) and requests >= 1
    if not called and measured is None:
        return "FAILED", "model_not_called"
    if _agent_patch(sandbox) is None:
        return "FAILED", "agent_no_change"
    if not passed and GRADING_TIMEOUT in _runner_details(sandbox):
        return "FAILED", GRADING_TIMEOUT
    if not passed and _suite_failure(sandbox) == CANDIDATE_FILES_TOO_LARGE:
        return "FAILED", CANDIDATE_FILES_TOO_LARGE
    return ("FINISHED" if passed else "FAILED"), (None if passed else "test_failed")


def _usage(result: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(result, dict):
        return None
    incoming = result.get("input_tokens")
    outgoing = result.get("output_tokens")
    if type(incoming) is not int or type(outgoing) is not int:
        return None
    if incoming < 0 or outgoing < 0 or incoming + outgoing < 1:
        return None
    return incoming, outgoing


def _request_detail(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
) -> dict[str, Any]:
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        raw = conn.execute(
            text(
                "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
            ),
            {"project_id": project_id, "task_id": task_id, "request_key": request_key},
        ).scalar()
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _measured_from_detail(detail: dict[str, Any]) -> tuple[int, int] | None:
    if "measured_input_tokens" not in detail or "measured_output_tokens" not in detail:
        return None
    incoming = detail["measured_input_tokens"]
    outgoing = detail["measured_output_tokens"]
    if type(incoming) is not int or type(outgoing) is not int or incoming < 0 or outgoing < 0 or incoming + outgoing < 1:
        return None
    return incoming, outgoing


def _relay_flags(detail: dict[str, Any]) -> tuple[str | None, bool]:
    error = detail.get("upstream_error")
    code = error if isinstance(error, str) and error else None
    return code, detail.get("usage_missing") is True


def _successful_model_requests(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
) -> int:
    """Gateway-observed calls. A sandbox exception must not publish this as zero."""
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                "AND role = 'coding_call' AND request_key LIKE :prefix AND usage_status = 'known' "
                "AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0 "
                "AND (usage_detail->>'upstream_error') IS NULL"
            ),
            {"project_id": project_id, "task_id": task_id, "prefix": request_key + ":call:%"},
        ).scalar()
    return int(count or 0)


def _log_job(
    *,
    job_id: str,
    fencing: int,
    state: str,
    error: str | None,
    result: dict[str, Any],
    measured: tuple[int, int] | None,
    patch_source: str,
) -> None:
    logger = _logger()
    logger.info(
        "coding_job job=%s fencing=%s state=%s error=%s model_requests=%s edit_calls=%s input_tokens=%s output_tokens=%s patch_source=%s"
        " result_line_bytes=%s",
        job_id,
        fencing,
        state,
        error or "",
        result.get("model_requests") if isinstance(result.get("model_requests"), int) else "",
        result.get("edit_calls") if isinstance(result.get("edit_calls"), int) else "",
        "" if measured is None else measured[0],
        "" if measured is None else measured[1],
        patch_source,
        # Set by the entry only when it dropped a patch too large for the RESULT line (patch_too_large).
        _result_line_bytes(result),
    )


def _result_line_bytes(result: dict[str, Any]) -> int | str:
    value = result.get("result_line_bytes")
    return value if type(value) is int and value > 0 else ""


def _logger() -> logging.Logger:
    _LOG.setLevel(logging.INFO)
    if not _LOG.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        _LOG.addHandler(handler)
    return _LOG


def _drain_proxy(
    proxy: CodingModelProxy,
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
    heartbeat: Callable[[], None] | None = None,
    fence: tuple[str, int] | None = None,
) -> None:
    """Wait for in-flight model calls; one that outlasts the drain is charged at its reservation.

    The tokens still pending on the reservation belong to calls that were admitted but never
    answered. They move to unknown_reserved_tokens, which _settle charges like a timeout.
    proxy_drained tells the gateway not to book a late answer on top of that charge.
    The heartbeat keeps the lease alive between drain slices; if it raises, nothing is written.
    """
    if heartbeat is None:
        unfinished = proxy.drain()
    else:
        unfinished = proxy.drain(heartbeat=heartbeat, heartbeat_seconds=DRAIN_RENEW_SECONDS)
    if unfinished <= 0:
        return
    _LOG.warning("coding_proxy_drain_timeout job_request=%s unfinished=%s", request_key, unfinished)
    with ledger.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        if fence is not None and not _fence_guard(project_id, *fence)(conn):
            raise _LeaseLost(fence[0])
        raw = conn.execute(
            text(
                "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
            ),
            {"project_id": project_id, "task_id": task_id, "request_key": request_key},
        ).scalar()
        if raw is None:
            return
        detail = dict(raw) if isinstance(raw, dict) else dict(json.loads(raw) or {})
        pending = detail.get("pending_tokens")
        pending = pending if type(pending) is int and pending > 0 else 0
        detail["proxy_drained"] = True
        detail["drain_unfinished"] = unfinished
        if pending:
            detail["unknown_usage_calls"] = int(detail.get("unknown_usage_calls") or 0) + unfinished
            detail["unknown_reserved_tokens"] = int(detail.get("unknown_reserved_tokens") or 0) + pending
            detail["pending_tokens"] = 0
            move_call_cost(detail, "pending", "unknown_reserved")
        conn.execute(
            text(
                "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
            ),
            {"detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
        )


def _settle(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
    reserved: bool,
    result: dict[str, Any] | None = None,
    fence: tuple[str, int] | None = None,
) -> None:
    """Book the attempt's reservation. With fence, only while the job still carries that token.

    A moved fence raises _LeaseLost and books nothing: the attempt that took the job closes this
    row itself (_settle_prior_attempts). Tokens still pending here belong to calls whose answer
    never reached the ledger after the drain; they are charged at the reservation like a timeout.
    """
    if not reserved:
        return
    guard = None if fence is None else _fence_guard(project_id, *fence)
    detail = _request_detail(
        ledger,
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        request_key=request_key,
    )
    usage = _measured_from_detail(detail)
    if usage is not None:
        # Money and tokens are booked apart: each measured call was priced at the reservation's
        # rate, rounded up on its own, when its usage arrived; this is their sum.
        actual = priced_micro_usd(detail, "measured", usage[0] + usage[1])
    else:
        usage = _usage(result)
        # Only the sandbox's total is known here, so it is priced as one call.
        actual = 0 if usage is None else coding_call_micro_usd(usage[0] + usage[1], detail)
    unknown = _unknown_reserved_tokens(detail)
    pending = _count_detail(detail.get("pending_tokens"))
    try:
        if usage is None:
            reason = _unsent_reason(detail)
            if reason is not None:
                # Nothing here can have been billed: give the whole hold back and say why.
                ledger.cancel_unused(
                    owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, guard=guard, reason=reason
                )
                return
            ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, guard=guard)
            return
        incoming, outgoing = usage
        ledger.settle_measured(
            owner_id=owner_id,
            project_id=project_id,
            task_id=task_id,
            request_key=request_key,
            actual=actual,
            input_tokens=incoming,
            output_tokens=outgoing,
            unknown_tokens=unknown + pending,
            # Each unknown call at its own reservation, rounded up per call, then summed.
            unknown_micro_usd=priced_micro_usd(detail, "unknown_reserved", unknown) + priced_micro_usd(detail, "pending", pending),
            guard=guard,
        )
    except BudgetError as exc:
        if exc.code == "fence_lost" and fence is not None:
            raise _LeaseLost(fence[0]) from exc
        if exc.code not in {"unknown_not_zero", "not_reserved", "conflicting_settlement"}:
            raise
    except NoResultFound:
        return


def _unsent_reason(detail: dict[str, Any]) -> str | None:
    """Why no call on this reservation can have been billed, or None when one may have been.

    Read only after the drain, when no call is still being forwarded. The gateway leaves
    measured, unknown or pending tokens (or usage_missing) for every call that may have reached
    the provider and been billed; a cut error body and a drain that gave up on a call count too.
    Without any of them, every call was refused before it was sent (sent=False, e.g.
    model_unavailable) or got a complete non-2xx reply, which is not billed (ADR-F46 R2).
    """
    if _measured_from_detail(detail) is not None or _unknown_reserved_tokens(detail) > 0:
        return None
    if _count_detail(detail.get("pending_tokens")) or _count_detail(detail.get("unknown_usage_calls")):
        return None
    if detail.get("usage_missing") is True or detail.get("error_body_truncated") is True:
        return None
    if detail.get("proxy_drained") is True or _count_detail(detail.get("drain_unfinished")):
        return None
    for key in ("admission_error", "upstream_error"):
        code = detail.get(key)
        if isinstance(code, str) and code:
            return code
    return "no_model_call"


def _unknown_reserved_tokens(detail: dict[str, Any]) -> int:
    """Reserved tokens of calls whose usage never arrived; the ledger charges them, capped at the hold.

    A row flagged usage_missing without the per-call amount (written before the gateway
    recorded it) cannot say how much was unknown, so the whole hold stays charged.
    """
    recorded = detail.get("unknown_reserved_tokens")
    if type(recorded) is int and recorded > 0:
        return recorded
    if detail.get("usage_missing") is True:
        held = detail.get("reserved_tokens")
        return held if type(held) is int and held > 0 else 0
    return 0


def _release_hold(
    ledger: BudgetLedger,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    request_key: str,
    reserved: bool,
    fence: tuple[str, int] | None = None,
) -> None:
    """Keep the hold as unknown. With fence, a moved fencing token leaves the row to the new holder."""
    if not reserved:
        return
    guard = None if fence is None else _fence_guard(project_id, *fence)
    try:
        ledger.mark_unknown(owner_id=owner_id, project_id=project_id, task_id=task_id, request_key=request_key, guard=guard)
    except BudgetError:
        return
