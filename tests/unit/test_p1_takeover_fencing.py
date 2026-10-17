"""A takeover moves the fencing token, and the earlier attempt's reservation is settled, not reused.

The claim of a job whose lease lapsed, and the reaper's take of an expired one, hand the job to a
new holder. Before this, both kept the fencing token, so the old worker (alive, its lease merely
lapsed) renewed again as soon as the new holder's lease was live, and could publish, settle and
clear the lease of a run that was no longer its own. The parent reservation is keyed
coding-{job}-{fencing}; with a new token each attempt gets its own row, and the earlier attempt's
row is closed at takeover: measured tokens at their amount, calls still pending at their
reservation, nothing at all when no call was ever admitted. The hold is never kept twice, never
dropped while a call may have been billed, and never charged past what it held.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from aether_gateway.model import _coding_lease_held
from aether_storage.artifacts import ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker import coding_jobs
from coding_sql_fake import CodingSqlFake, Store
from ledger_sql_fake import FakeLedgerDatabase

_OWNER = "owner-1"
_PROJECT = "project-1"
_TASK = "task-1"
_JOB = "00000000-0000-0000-0000-0000000000f1"
_OPERATION = "00000000-0000-0000-0000-0000000000f2"
_OLD_FENCE = 3
_RESERVE = 4096
_RESULT = {"exit_code": 0, "result": {"passed": 1, "failed": 0, "agent_completed": True, "patch": "--- a/x\n"}}


def _db(*, limit: int = 2_000_000, deadline: float | None = None, lease_until: float | None = None, state: str = "QUEUED") -> CodingSqlFake:
    db = CodingSqlFake()
    db.add_account(_PROJECT, _TASK, limit_micro_usd=limit)
    db.add_job(
        _JOB,
        project_id=_PROJECT,
        task_id=_TASK,
        operation_id=_OPERATION,
        fencing=_OLD_FENCE,
        state=state,
        deadline=deadline,
        lease_until=lease_until,
    )
    return db


def _key(fencing: int) -> str:
    return f"coding-{_JOB}-{fencing}"


def _old_attempt(db: CodingSqlFake, **detail: Any) -> dict[str, Any]:
    """The reservation the old worker made, exactly as _run_one makes it, plus what its calls recorded."""
    assert BudgetLedger(cast(Database, db)).reserve(
        owner_id=_OWNER,
        project_id=_PROJECT,
        task_id=_TASK,
        request_key=_key(_OLD_FENCE),
        amount=_RESERVE,
        role="coding",
        tokens=_RESERVE,
        extra_detail={"call_reserve_tokens": _RESERVE, "call_reserve_micro_usd": _RESERVE},
    )
    row = db.request(_PROJECT, _TASK, _key(_OLD_FENCE))
    row["usage_detail"].update(detail)
    return row


def _renew(db: CodingSqlFake, fencing: int) -> bool:
    return coding_jobs._renew_lease(
        cast(Database, db), owner_id=_OWNER, project_id=_PROJECT, operation_id=_OPERATION, job_id=_JOB, fencing_token=fencing
    )


class _Proxy:
    def __init__(self, _database: Any, *, job_token: str) -> None:
        self.directory = Path("/tmp")
        self.job_token = job_token

    # The sandbox entry revoked the channel in band before its container exited, and the
    # worker's own revoke after the container returned is a no-op, as on CodingModelProxy.
    revoked_in_band = True

    def revoke(self) -> None:
        return None

    def drain(self, **_kwargs: Any) -> int:
        return 0

    def close(self) -> None:
        return None


class _Run:
    """_run_one against the fake database; the sandbox, proxy, workspace and publish are stand-ins."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, db: CodingSqlFake) -> None:
        self.db = db
        self.published: list[int] = []
        self.during: list[Any] = []
        self.inside: Any = None
        monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_TOKENS", raising=False)
        monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", raising=False)
        monkeypatch.setattr(coding_jobs, "CodingModelProxy", _Proxy)
        monkeypatch.setattr(coding_jobs, "_cancel_requested", lambda *_a, **_k: False)
        monkeypatch.setattr(coding_jobs, "_deadline_passed", lambda *_a, **_k: False)
        monkeypatch.setattr(coding_jobs, "_workspace", lambda *_a, **_k: {"base_files": {}, "files": {}, "tests": {}})
        monkeypatch.setattr(coding_jobs, "_ensure_account", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_materialize", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_issue_coding_token", lambda *_a, **_k: "job-token-takeover")
        monkeypatch.setattr(coding_jobs, "_operation", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_manifest", lambda *_a, **_k: [])
        monkeypatch.setattr(coding_jobs, "_coding_terminal", lambda *_a, **_k: ("FINISHED", None))
        monkeypatch.setattr(coding_jobs, "_publish", lambda *_a, row, **_k: self.published.append(int(row["fencing_token"])) or True)
        monkeypatch.setattr(coding_jobs, "run_coding_sandbox", self.sandbox)

    def sandbox(self, _root: Path, **kwargs: Any) -> dict[str, Any]:
        self.during.append(
            {
                "fencing": self.db.jobs[_JOB]["fencing_token"],
                "requests": {key[2]: dict(row, usage_detail=dict(row["usage_detail"])) for key, row in self.db.requests.items()},
                "account": dict(self.db.account(_PROJECT, _TASK)),
            }
        )
        if self.inside is not None:
            inside, self.inside = self.inside, None
            inside(kwargs)
        return _RESULT

    def run(self) -> dict[str, str]:
        return coding_jobs._run_one(
            cast(Database, self.db), cast(ArtifactStore, Store()), owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB
        )


# 1. The fence itself


def test_a_claim_after_the_lease_lapsed_moves_the_fence_so_the_old_worker_can_no_longer_renew_clear_or_publish() -> None:
    db = _db()
    database = cast(Database, db)
    old = coding_jobs._claim(database, owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB)
    assert old is not None
    db.now += coding_jobs.LEASE_SECONDS + 1  # the old worker stalled; its lease lapsed
    new = coding_jobs._claim(database, owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB)
    assert new is not None, "the lapsed lease must be claimable"
    old_fence, new_fence = int(old["fencing_token"]), int(new["fencing_token"])

    assert _renew(db, old_fence) is False, "the old worker renewed the lease the new worker now holds"
    assert _renew(db, new_fence) is True
    assert new_fence > old_fence
    assert db.jobs[_JOB]["fencing_token"] == new_fence
    assert db.operations[_OPERATION]["fencing_token"] == new_fence

    held = db.operations[_OPERATION]["lease_until"]
    coding_jobs._clear_lease(database, owner_id=_OWNER, project_id=_PROJECT, operation_id=_OPERATION, job_id=_JOB, fencing_token=old_fence)
    assert db.operations[_OPERATION]["lease_until"] == held, "the old worker cleared the new worker's lease"

    # The old run's proxy token carries the old fence: the gateway no longer admits its calls.
    assert _coding_lease_held(db, project_id=_PROJECT, task_id=_TASK, job_id=_JOB, operation_id=_OPERATION, fencing=old_fence) is False
    assert _coding_lease_held(db, project_id=_PROJECT, task_id=_TASK, job_id=_JOB, operation_id=_OPERATION, fencing=new_fence) is True

    stored = coding_jobs._store_terminal(
        database,
        cast(ArtifactStore, Store()),
        owner_id=_OWNER,
        project_id=_PROJECT,
        row=old,
        state="FINISHED",
        error_code=None,
        patch=b"--- a/x\n",
        result=b"{}",
        events=b"{}",
        revoke=False,
    )
    assert stored is False, "the old worker published over the new worker's run"
    assert db.jobs[_JOB]["state"] == "STARTING"
    assert db.operations[_OPERATION]["state"] == "PREPARED"


def test_the_reaper_taking_an_expired_job_also_moves_the_fence_and_settles_the_old_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(deadline=900.0, lease_until=999.0, state="ACTIVE")
    _old_attempt(db, measured_input_tokens=1000, measured_output_tokens=200, pending_tokens=2000)
    stopped: list[int] = []
    monkeypatch.setattr(
        coding_jobs, "_stop_confirmed", lambda *_a, row, **_k: stopped.append(int(row["fencing_token"])) or None
    )
    coding_jobs.reap_expired_coding_jobs(cast(Database, db), cast(ArtifactStore, Store()), owner_id=_OWNER, project_id=_PROJECT)
    assert stopped, "the reaper did not take the expired job"
    assert _renew(db, _OLD_FENCE) is False, "the old worker renewed the lease the reaper now holds"
    assert stopped == [db.jobs[_JOB]["fencing_token"]] and stopped[0] > _OLD_FENCE
    row = db.request(_PROJECT, _TASK, _key(_OLD_FENCE))
    assert row["usage_status"] == "known", "the old attempt's hold was left on the account"
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)
    assert row["usage_detail"]["unknown_reserved_tokens"] == 2000
    account = db.account(_PROJECT, _TASK)
    assert (account["spent_micro_usd"], account["held_micro_usd"]) == (1200 + 2000, 0)
    assert (account["tokens_used"], account["tokens_held"]) == (1200 + 2000, 0)


# 2. The old attempt's reservation at takeover


def test_a_takeover_settles_the_old_attempt_measured_plus_pending_at_its_reservation_and_holds_only_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(lease_until=999.0)  # the old worker's lease already lapsed
    _old_attempt(db, measured_input_tokens=1000, measured_output_tokens=200, pending_tokens=2000)
    run = _Run(monkeypatch, db)
    outcome = run.run()
    assert outcome["state"] == "FINISHED", outcome
    seen = run.during[0]
    old = seen["requests"][_key(_OLD_FENCE)]
    assert old["usage_status"] == "known", "the old attempt's reservation was reused or left held"
    assert (old["input_tokens"], old["output_tokens"]) == (1000, 200)
    # The calls still pending when the old worker stopped are charged at their reservation.
    assert old["usage_detail"]["unknown_reserved_tokens"] == 2000
    assert old["usage_detail"]["pending_tokens"] == 0
    assert old["usage_detail"]["proxy_drained"] is True
    new_key = _key(seen["fencing"])
    assert new_key != _key(_OLD_FENCE) and seen["requests"][new_key]["usage_status"] == "reserved"
    account = seen["account"]
    assert account["spent_micro_usd"] == 1200 + 2000
    assert account["tokens_used"] == 1200 + 2000
    # Only the new attempt's reservation is held: nothing is frozen twice.
    assert (account["held_micro_usd"], account["tokens_held"]) == (_RESERVE, _RESERVE)
    assert run.published == [seen["fencing"]]


def test_the_takeover_charge_for_pending_calls_never_exceeds_what_the_old_reservation_held(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(lease_until=999.0)
    _old_attempt(db, measured_input_tokens=1000, measured_output_tokens=200, pending_tokens=3000)
    run = _Run(monkeypatch, db)
    run.run()
    seen = run.during[0]
    old = seen["requests"][_key(_OLD_FENCE)]
    assert old["usage_status"] == "known"
    assert old["actual_micro_usd"] == _RESERVE
    assert seen["account"]["spent_micro_usd"] == _RESERVE
    assert seen["account"]["tokens_used"] == _RESERVE
    assert seen["account"]["held_micro_usd"] == _RESERVE


def test_an_old_attempt_that_never_admitted_a_call_is_released_not_frozen_beside_the_new_one(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(lease_until=999.0)
    _old_attempt(db)
    run = _Run(monkeypatch, db)
    run.run()
    seen = run.during[0]
    old = seen["requests"][_key(_OLD_FENCE)]
    assert old["usage_status"] == "cancelled_before_dispatch", old["usage_status"]
    assert seen["account"]["spent_micro_usd"] == 0
    assert (seen["account"]["held_micro_usd"], seen["account"]["tokens_held"]) == (_RESERVE, _RESERVE)


def test_after_settling_the_old_attempt_the_new_reservation_still_respects_the_budget_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ceiling 0.8 * 10000 = 8000: the old attempt's 4096 is spent, another 4096 would pass the ceiling.
    db = _db(limit=10_000, lease_until=999.0)
    _old_attempt(db, measured_input_tokens=4000, measured_output_tokens=96)
    run = _Run(monkeypatch, db)
    outcome = run.run()
    assert outcome.get("error_code") == "budget_exceeded", outcome
    assert run.during == []
    account = db.account(_PROJECT, _TASK)
    assert db.request(_PROJECT, _TASK, _key(_OLD_FENCE))["usage_status"] == "known"
    assert account["spent_micro_usd"] == 4096
    assert account["held_micro_usd"] == 0
    assert account["spent_micro_usd"] + account["held_micro_usd"] <= 8000


# 3. The old worker after the takeover


def test_an_old_worker_that_finishes_after_the_takeover_neither_settles_nor_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db()
    run = _Run(monkeypatch, db)
    taken: list[dict[str, Any]] = []

    def take_over(_kwargs: dict[str, Any]) -> None:
        # While the old worker's agent runs, its calls are measured on its reservation; then it
        # stalls past its lease and another worker claims the job.
        key = _key(int(db.jobs[_JOB]["fencing_token"]))
        db.request(_PROJECT, _TASK, key)["usage_detail"].update(measured_input_tokens=100, measured_output_tokens=50)
        db.now += coding_jobs.LEASE_SECONDS + 1
        claimed = coding_jobs._claim(cast(Database, db), owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB)
        assert claimed is not None
        taken.append(claimed)

    run.inside = take_over
    outcome = run.run()
    old_key = _key(int(run.during[0]["fencing"]))
    assert outcome == {"coding_job_id": _JOB, "state": "ACTIVE", "error_code": "fence_lost"}, outcome
    assert run.published == [], "the old worker published after the takeover"
    row = db.request(_PROJECT, _TASK, old_key)
    # Left for the new holder's takeover settlement, untouched by the old worker.
    assert row["usage_status"] == "reserved", "the old worker settled its reservation after the takeover"
    assert row["usage_detail"]["measured_input_tokens"] == 100
    assert db.account(_PROJECT, _TASK)["spent_micro_usd"] == 0


# 4. Pending tokens at the final settlement


def test_tokens_still_pending_at_the_final_settlement_are_charged_at_their_reservation() -> None:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=8192, tokens_held=8192, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        "coding-final",
        reserved_micro_usd=8192,
        usage_detail={
            "reserved_tokens": 8192,
            "call_reserve_tokens": 4096,
            "call_reserve_micro_usd": 4096,
            "measured_input_tokens": 100,
            "measured_output_tokens": 50,
            # A call whose answer never reached the ledger (the gateway lost it) and no drain timed out.
            "pending_tokens": 3000,
        },
    )
    coding_jobs._settle(
        BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key="coding-final", reserved=True
    )
    row = db.request(_PROJECT, _TASK, "coding-final")
    account = db.account(_PROJECT, _TASK)
    assert row["usage_status"] == "known"
    assert row["usage_detail"]["unknown_charged_tokens"] == 3000
    assert account["spent_micro_usd"] == 150 + 3000
    assert account["tokens_used"] == 150 + 3000
    assert account["held_micro_usd"] == 0
