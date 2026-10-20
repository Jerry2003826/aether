"""The takeover fencing and settlement of test_p1_takeover_fencing, against real PostgreSQL.

The unit tests run this SQL on an in-memory imitation. Here the same eight scenarios run on the
real schema, roles and row-level security: the claim and the reaper's take moving the fence, the
settlement of an earlier attempt's parent row (FOR SHARE fence guard, jsonb updates, the cap at the
hold), the final settlement charging pending tokens, and the old worker failing to renew, clear,
publish or settle once the fence moved. The lease clock is moved by writing lease_until.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from aether_gateway.model import _coding_lease_held
from aether_storage.artifacts import ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker import coding_jobs
from aether_worker.console_commands import apply_console_commands
from sqlalchemy import text

from tests.integration.test_console_records import _headers, _session

WORKSPACE = {
    "kind": "aether.coding_workspace",
    "base_files": {"app.py": "def value():\n    return 0\n"},
    "files": {"app.py": "def value():\n    return 1\n"},
    "tests": {"test_app.py": "from app import value\n\ndef test_value():\n    assert value() == 42\n"},
}
_RESULT = {"exit_code": 0, "result": {"passed": 1, "failed": 0, "agent_completed": True, "patch": "--- a/x\n"}}


# Coding reservations record their prices (ADR-F49): measured tokens at 1 micro-USD each, input and output alike.
_PRICES = {"pricing_version": "test-1000000-1000000", "input_micro_usd_per_mtok": 1_000_000, "output_micro_usd_per_mtok": 1_000_000}

class _Job:
    """One queued coding job created through the API, with its budget account open."""

    def __init__(self, client: Any, role_urls: dict[str, str], aether_admin: str, name: str, limit: int) -> None:
        self.owner_id, self.project_id, csrf = _session(client, f"pg-fence-{name}-project")
        task = client.post(f"/v1/projects/{self.project_id}/tasks", headers=_headers(csrf, f"pg-fence-{name}-task"), json={"goal": "接管移动 fence"})
        assert task.status_code == 202, task.text
        self.task_id = str(task.json()["resource_id"])
        uploaded = client.post(
            f"/v1/projects/{self.project_id}/artifacts",
            headers=_headers(csrf, f"pg-fence-{name}-file"),
            files={"file": ("base.json", json.dumps(WORKSPACE).encode(), "application/json")},
        )
        assert uploaded.status_code == 201, uploaded.text
        created = client.post(
            f"/v1/projects/{self.project_id}/coding-jobs",
            headers=_headers(csrf, f"pg-fence-{name}-job"),
            json={"task_id": self.task_id, "base_artifact_id": uploaded.json()["artifact_id"], "job_key": name, "objective": "接管"},
        )
        assert created.status_code == 202, created.text
        self.job_id = str(created.json()["resource_id"])
        self.db = Database(role_urls["aether_worker"])
        self.store = ArtifactStore(client.app.state.settings.artifact_root, self.db)
        apply_console_commands(self.db, owner_id=self.owner_id, project_id=self.project_id, store=self.store)
        self.admin = aether_admin
        job = self.job()
        assert job["state"] == "QUEUED", job
        self.operation_id = str(job["operation_id"])
        self.fence = int(job["fencing_token"])
        with psycopg.connect(aether_admin, autocommit=True) as conn:
            conn.execute("DELETE FROM budget_accounts WHERE project_id = %s AND task_id = %s", (self.project_id, self.task_id))
        BudgetLedger(self.db).open_account(
            owner_id=self.owner_id,
            project_id=self.project_id,
            task_id=self.task_id,
            limit_micro_usd=limit,
            token_limit=10_000_000,
            max_requests=100,
        )
        self.reserve_tokens = coding_jobs.coding_call_reserve_tokens()
        self.reserve_micro = coding_jobs.coding_call_reserve_micro_usd()

    def key(self, fencing: int) -> str:
        return f"coding-{self.job_id}-{fencing}"

    def _one(self, sql: str, **params: Any) -> dict[str, Any]:
        row = self._first(sql, **params)
        assert row is not None, params
        return row

    def _first(self, sql: str, **params: Any) -> dict[str, Any] | None:
        with self.db.transaction(owner_id=self.owner_id, project_id=self.project_id) as conn:
            row = conn.execute(text(sql), {"project_id": self.project_id, **params}).mappings().first()
        return None if row is None else dict(row)

    def job(self) -> dict[str, Any]:
        return self._one(
            "SELECT j.state, j.fencing_token, j.operation_id::text AS operation_id, o.fencing_token AS operation_fencing, "
            "o.lease_until, o.state AS operation_state FROM coding_jobs j JOIN operations o "
            "ON o.project_id = j.project_id AND o.id = j.operation_id WHERE j.project_id = :project_id AND j.id = :id",
            id=self.job_id,
        )

    def request(self, key: str) -> dict[str, Any]:
        row = self.request_or_none(key)
        assert row is not None, key
        return row

    def request_or_none(self, key: str) -> dict[str, Any] | None:
        return self._first(
            "SELECT usage_status, actual_micro_usd, input_tokens, output_tokens, usage_detail FROM model_requests "
            "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :key",
            task_id=self.task_id,
            key=key,
        )

    def account(self) -> dict[str, Any]:
        return self._one(
            "SELECT spent_micro_usd, held_micro_usd, tokens_used, tokens_held FROM budget_accounts "
            "WHERE project_id = :project_id AND task_id = :task_id",
            task_id=self.task_id,
        )

    def admin_sql(self, sql: str, *params: Any) -> None:
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(sql, params)

    def lapse_lease(self) -> None:
        self.admin_sql("UPDATE operations SET lease_until = now() - interval '1 second' WHERE project_id = %s AND id = %s", self.project_id, self.operation_id)

    def record(self, key: str, **detail: Any) -> None:
        """What the gateway wrote on the parent row while the attempt's calls ran."""
        self.admin_sql(
            "UPDATE model_requests SET usage_detail = usage_detail || %s::jsonb WHERE project_id = %s AND task_id = %s AND request_key = %s",
            json.dumps(detail),
            self.project_id,
            self.task_id,
            key,
        )

    def old_attempt(self, **detail: Any) -> str:
        """The reservation the old worker made at the current fence, exactly as _run_one makes it."""
        key = self.key(self.fence)
        assert BudgetLedger(self.db).reserve(
            owner_id=self.owner_id,
            project_id=self.project_id,
            task_id=self.task_id,
            request_key=key,
            amount=self.reserve_micro,
            role="coding",
            tokens=self.reserve_tokens,
            extra_detail={"call_reserve_tokens": self.reserve_tokens, "call_reserve_micro_usd": self.reserve_micro, **_PRICES},
        )
        if detail:
            self.record(key, **detail)
        return key

    def renew(self, fencing: int) -> bool:
        return coding_jobs._renew_lease(
            self.db, owner_id=self.owner_id, project_id=self.project_id, operation_id=self.operation_id, job_id=self.job_id, fencing_token=fencing
        )

    def lease_held(self, fencing: int) -> bool:
        with self.db.transaction(owner_id=self.owner_id, project_id=self.project_id) as conn:
            return _coding_lease_held(
                conn, project_id=self.project_id, task_id=self.task_id, job_id=self.job_id, operation_id=self.operation_id, fencing=fencing
            )


class _Run:
    """_run_one on the real database; only the sandbox, proxy, token, workspace files and publish are stand-ins."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, job: _Job) -> None:
        self.job = job
        self.published: list[int] = []
        self.during: list[dict[str, Any]] = []
        self.inside: Any = None

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

        monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_TOKENS", raising=False)
        monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", raising=False)
        monkeypatch.setattr(coding_jobs, "CodingModelProxy", _Proxy)
        monkeypatch.setattr(coding_jobs, "_ensure_account", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_materialize", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_issue_coding_token", lambda *_a, **_k: "job-token-takeover")
        monkeypatch.setattr(coding_jobs, "_manifest", lambda *_a, **_k: [])
        monkeypatch.setattr(coding_jobs, "_coding_terminal", lambda *_a, **_k: ("FINISHED", None))
        monkeypatch.setattr(coding_jobs, "_publish", lambda *_a, row, **_k: self.published.append(int(row["fencing_token"])) or True)
        monkeypatch.setattr(coding_jobs, "run_coding_sandbox", self.sandbox)

    def sandbox(self, _root: Path, **kwargs: Any) -> dict[str, Any]:
        job = self.job
        fencing = int(job.job()["fencing_token"])
        self.during.append(
            {
                "fencing": fencing,
                "old": job.request_or_none(job.key(job.fence)),
                "new": job.request(job.key(fencing)),
                "account": job.account(),
            }
        )
        if self.inside is not None:
            inside, self.inside = self.inside, None
            inside(fencing)
        return _RESULT

    def run(self) -> dict[str, str]:
        return coding_jobs._run_one(self.job.db, self.job.store, owner_id=self.job.owner_id, project_id=self.job.project_id, job_id=self.job.job_id)


@pytest.fixture
def make_job(client, role_urls, aether_admin):
    def make(name: str, *, limit: int = 2_000_000) -> _Job:
        return _Job(client, role_urls, aether_admin, name, limit)

    return make


# 1. The fence itself


def test_a_claim_after_the_lease_lapsed_moves_the_fence_so_the_old_worker_can_no_longer_renew_clear_or_publish(make_job) -> None:
    job = make_job("claim")
    old = coding_jobs._claim(job.db, owner_id=job.owner_id, project_id=job.project_id, job_id=job.job_id)
    assert old is not None
    job.lapse_lease()
    new = coding_jobs._claim(job.db, owner_id=job.owner_id, project_id=job.project_id, job_id=job.job_id)
    assert new is not None, "the lapsed lease must be claimable"
    old_fence, new_fence = int(old["fencing_token"]), int(new["fencing_token"])
    assert old_fence == job.fence + 1 and new_fence == old_fence + 1

    assert job.renew(old_fence) is False, "the old worker renewed the lease the new worker now holds"
    assert job.renew(new_fence) is True
    row = job.job()
    assert (int(row["fencing_token"]), int(row["operation_fencing"])) == (new_fence, new_fence)

    held = row["lease_until"]
    coding_jobs._clear_lease(
        job.db, owner_id=job.owner_id, project_id=job.project_id, operation_id=job.operation_id, job_id=job.job_id, fencing_token=old_fence
    )
    assert job.job()["lease_until"] == held, "the old worker cleared the new worker's lease"

    assert job.lease_held(old_fence) is False
    assert job.lease_held(new_fence) is True

    stored = coding_jobs._store_terminal(
        job.db,
        job.store,
        owner_id=job.owner_id,
        project_id=job.project_id,
        row=old,
        state="FINISHED",
        error_code=None,
        patch=b"--- a/x\n",
        result=b"{}",
        events=b"{}",
        revoke=False,
    )
    assert stored is False, "the old worker published over the new worker's run"
    after = job.job()
    assert after["state"] == "STARTING"
    assert after["operation_state"] == row["operation_state"]


def test_the_reaper_taking_an_expired_job_also_moves_the_fence_and_settles_the_old_attempt(make_job, monkeypatch) -> None:
    job = make_job("reaper")
    old_key = job.old_attempt(measured_input_tokens=1000, measured_output_tokens=200, pending_tokens=2000)
    job.admin_sql(
        "UPDATE coding_jobs SET state = 'ACTIVE', deadline = now() - interval '1 minute' WHERE project_id = %s AND id = %s",
        job.project_id,
        job.job_id,
    )
    job.lapse_lease()
    stopped: list[int] = []
    monkeypatch.setattr(coding_jobs, "_stop_confirmed", lambda *_a, row, **_k: stopped.append(int(row["fencing_token"])) or None)
    coding_jobs.reap_expired_coding_jobs(job.db, job.store, owner_id=job.owner_id, project_id=job.project_id)
    assert stopped, "the reaper did not take the expired job"
    assert job.renew(job.fence) is False, "the old worker renewed the lease the reaper now holds"
    assert stopped == [int(job.job()["fencing_token"])] and stopped[0] == job.fence + 1
    row = job.request(old_key)
    assert row["usage_status"] == "known", "the old attempt's hold was left on the account"
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)
    assert row["usage_detail"]["unknown_reserved_tokens"] == 2000
    assert row["usage_detail"]["superseded_by_fencing"] == job.fence + 1
    account = job.account()
    assert (account["spent_micro_usd"], account["held_micro_usd"]) == (1200 + 2000, 0)
    assert (account["tokens_used"], account["tokens_held"]) == (1200 + 2000, 0)


# 2. The old attempt's reservation at takeover


def test_a_takeover_settles_the_old_attempt_measured_plus_pending_at_its_reservation_and_holds_only_its_own(make_job, monkeypatch) -> None:
    job = make_job("settle")
    job.old_attempt(measured_input_tokens=1000, measured_output_tokens=200, pending_tokens=2000)
    run = _Run(monkeypatch, job)
    outcome = run.run()
    assert outcome["state"] == "FINISHED", outcome
    seen = run.during[0]
    assert seen["fencing"] == job.fence + 1
    old = seen["old"]
    assert old["usage_status"] == "known", "the old attempt's reservation was reused or left held"
    assert (old["input_tokens"], old["output_tokens"]) == (1000, 200)
    assert old["usage_detail"]["unknown_reserved_tokens"] == 2000
    assert old["usage_detail"]["pending_tokens"] == 0
    assert old["usage_detail"]["proxy_drained"] is True
    assert seen["new"]["usage_status"] == "reserved"
    account = seen["account"]
    assert account["spent_micro_usd"] == 1200 + 2000
    assert account["tokens_used"] == 1200 + 2000
    assert (account["held_micro_usd"], account["tokens_held"]) == (job.reserve_micro, job.reserve_tokens)
    assert run.published == [seen["fencing"]]


def test_the_takeover_charge_for_pending_calls_never_exceeds_what_the_old_reservation_held(make_job, monkeypatch) -> None:
    job = make_job("cap")
    assert job.reserve_tokens == job.reserve_micro == 4096
    job.old_attempt(measured_input_tokens=1000, measured_output_tokens=200, pending_tokens=3000)
    run = _Run(monkeypatch, job)
    run.run()
    seen = run.during[0]
    assert seen["old"]["usage_status"] == "known"
    assert seen["old"]["actual_micro_usd"] == job.reserve_micro
    assert seen["account"]["spent_micro_usd"] == job.reserve_micro
    assert seen["account"]["tokens_used"] == job.reserve_tokens
    assert seen["account"]["held_micro_usd"] == job.reserve_micro


def test_an_old_attempt_that_never_admitted_a_call_is_released_not_frozen_beside_the_new_one(make_job, monkeypatch) -> None:
    job = make_job("unused")
    job.old_attempt()
    run = _Run(monkeypatch, job)
    run.run()
    seen = run.during[0]
    assert seen["old"]["usage_status"] == "cancelled_before_dispatch", seen["old"]["usage_status"]
    assert seen["account"]["spent_micro_usd"] == 0
    assert (seen["account"]["held_micro_usd"], seen["account"]["tokens_held"]) == (job.reserve_micro, job.reserve_tokens)


def test_after_settling_the_old_attempt_the_new_reservation_still_respects_the_budget_ceiling(make_job, monkeypatch) -> None:
    # Ceiling 0.8 * 10000 = 8000: the old attempt's 4096 is spent, another 4096 would pass the ceiling.
    job = make_job("ceiling", limit=10_000)
    old_key = job.old_attempt(measured_input_tokens=4000, measured_output_tokens=96)
    run = _Run(monkeypatch, job)
    outcome = run.run()
    assert outcome.get("error_code") == "budget_exceeded", outcome
    assert run.during == []
    account = job.account()
    assert job.request(old_key)["usage_status"] == "known"
    assert account["spent_micro_usd"] == 4096
    assert account["held_micro_usd"] == 0
    assert account["spent_micro_usd"] + account["held_micro_usd"] <= 8000


# 3. The old worker after the takeover


def test_an_old_worker_that_finishes_after_the_takeover_neither_settles_nor_publishes(make_job, monkeypatch) -> None:
    job = make_job("late")
    run = _Run(monkeypatch, job)
    taken: list[dict[str, Any]] = []

    def take_over(fencing: int) -> None:
        job.record(job.key(fencing), measured_input_tokens=100, measured_output_tokens=50)
        job.lapse_lease()
        claimed = coding_jobs._claim(job.db, owner_id=job.owner_id, project_id=job.project_id, job_id=job.job_id)
        assert claimed is not None
        taken.append(claimed)

    run.inside = take_over
    outcome = run.run()
    old_key = job.key(run.during[0]["fencing"])
    assert outcome == {"coding_job_id": job.job_id, "state": "ACTIVE", "error_code": "fence_lost"}, outcome
    assert run.published == [], "the old worker published after the takeover"
    assert int(job.job()["fencing_token"]) == int(taken[0]["fencing_token"]) == run.during[0]["fencing"] + 1
    row = job.request(old_key)
    assert row["usage_status"] == "reserved", "the old worker settled its reservation after the takeover"
    assert row["usage_detail"]["measured_input_tokens"] == 100
    assert job.account()["spent_micro_usd"] == 0


# 4. Pending tokens at the final settlement


def test_tokens_still_pending_at_the_final_settlement_are_charged_at_their_reservation(make_job) -> None:
    job = make_job("final")
    key = "coding-final"
    assert BudgetLedger(job.db).reserve(
        owner_id=job.owner_id,
        project_id=job.project_id,
        task_id=job.task_id,
        request_key=key,
        amount=8192,
        role="coding",
        tokens=8192,
        extra_detail={"call_reserve_tokens": 4096, "call_reserve_micro_usd": 4096, **_PRICES},
    )
    job.record(key, measured_input_tokens=100, measured_output_tokens=50, pending_tokens=3000)
    coding_jobs._settle(BudgetLedger(job.db), owner_id=job.owner_id, project_id=job.project_id, task_id=job.task_id, request_key=key, reserved=True)
    row = job.request(key)
    account = job.account()
    assert row["usage_status"] == "known"
    assert row["usage_detail"]["unknown_charged_tokens"] == 3000
    assert account["spent_micro_usd"] == 150 + 3000
    assert account["tokens_used"] == 150 + 3000
    assert account["held_micro_usd"] == 0
