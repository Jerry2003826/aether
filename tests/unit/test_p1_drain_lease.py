"""The job keeps its lease while the model proxy drains, and a lease lost during the drain is not settled."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker import coding_jobs, coding_model_proxy
from aether_worker.coding_model_proxy import DEFAULT_DRAIN_SECONDS, CodingModelProxy

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_JOB = "00000000-0000-0000-0000-0000000000bb"
_FENCE = 9


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _Job:
    """_run_one with every collaborator faked; the proxy waits on a fake clock and never sees its call finish."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, renewals: list[bool] | None = None, fails: str | None = None) -> None:
        self.clock = _Clock()
        self.renewed_at: list[float] = []
        self.renewals = list(renewals) if renewals is not None else None
        self.waited: list[float] = []
        self.settled: list[float] = []
        self.published: list[str] = []
        self.stopped: list[str] = []
        self.released: list[str] = []
        self.drains = 0
        self.closed = 0
        self.fails = fails
        job = self
        row = {"id": _JOB, "fencing_token": _FENCE, "task_id": _TASK, "operation_id": "op-1", "base_artifact_id": "a-1"}
        monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", str(DEFAULT_DRAIN_SECONDS))
        monkeypatch.setattr(coding_model_proxy, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        monkeypatch.setattr(coding_jobs, "BudgetLedger", lambda database: SimpleNamespace(database=database, reserve=lambda **_k: True))
        monkeypatch.setattr(coding_jobs, "_claim", lambda *_a, **_k: dict(row))
        monkeypatch.setattr(coding_jobs, "_stop_confirmed", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_job_objective", lambda _row: "Make value() return 42.")
        monkeypatch.setattr(coding_jobs, "_workspace", lambda *_a, **_k: {})
        monkeypatch.setattr(coding_jobs, "_ensure_account", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_mark", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_operation", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_materialize", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_issue_coding_token", lambda *_a, **_k: "job-token-lease")
        monkeypatch.setattr(coding_jobs, "_renew_lease", self.renew)
        monkeypatch.setattr(coding_jobs, "_manifest", lambda *_a, **_k: [])
        monkeypatch.setattr(coding_jobs, "_request_detail", lambda *_a, **_k: {})
        monkeypatch.setattr(coding_jobs, "_successful_model_requests", lambda *_a, **_k: 1)
        monkeypatch.setattr(coding_jobs, "_coding_terminal", lambda *_a, **_k: ("FINISHED", None))
        monkeypatch.setattr(coding_jobs, "_publish", lambda *_a, **_k: job.published.append("publish") or True)
        monkeypatch.setattr(coding_jobs, "_settle", lambda *_a, **_k: job.settled.append(job.clock.now))
        monkeypatch.setattr(coding_jobs, "_release_hold", lambda *_a, **_k: job.released.append("release"))
        monkeypatch.setattr(coding_jobs, "_clear_lease", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_fail", lambda *_a, **_k: None)
        monkeypatch.setattr(
            coding_jobs,
            "_sandbox_stop",
            lambda *_a, **_k: job.stopped.append("stop") or {"coding_job_id": _JOB, "state": "FAILED", "error_code": "sandbox_failed"},
        )
        monkeypatch.setattr(coding_jobs, "run_coding_sandbox", self.sandbox)
        monkeypatch.setattr(CodingModelProxy, "wait_until_active", lambda proxy, count, *, timeout: job.wait(count, timeout))
        real_drain = CodingModelProxy.drain
        real_close = CodingModelProxy.close

        def drain(proxy: CodingModelProxy, **kwargs: Any) -> int:
            job.drains += 1
            return real_drain(proxy, **kwargs)

        def close(proxy: CodingModelProxy) -> None:
            job.closed += 1
            real_close(proxy)

        monkeypatch.setattr(CodingModelProxy, "drain", drain)
        monkeypatch.setattr(CodingModelProxy, "close", close)

    def renew(self, *_args: Any, **_kwargs: Any) -> bool:
        self.renewed_at.append(self.clock.now)
        if self.renewals is None:
            return True
        return self.renewals.pop(0) if self.renewals else False

    def wait(self, count: int, timeout: float) -> bool:
        # A call that never answers: every wait runs to its timeout on the fake clock.
        assert count == 0
        self.waited.append(timeout)
        self.clock.now += timeout
        return False

    def sandbox(self, _root: Path, **_kwargs: Any) -> dict[str, Any]:
        if self.fails is not None:
            raise coding_jobs.CodingSandboxError(self.fails)
        return {"exit_code": 0, "result": {"passed": 1, "failed": 0, "agent_completed": True, "patch": ""}}

    def run(self) -> dict[str, str]:
        return coding_jobs._run_one(cast(Database, object()), cast(ArtifactStore, object()), owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB)


def test_the_lease_is_renewed_while_the_drain_waits_its_full_time(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _Job(monkeypatch)
    outcome = job.run()
    assert outcome == {"coding_job_id": _JOB, "state": "FINISHED"}
    # The drain still waits the whole configured time; nothing shortens it.
    assert sum(job.waited) == pytest.approx(DEFAULT_DRAIN_SECONDS)
    assert job.clock.now == pytest.approx(DEFAULT_DRAIN_SECONDS)
    during = [moment for moment in job.renewed_at if 0 < moment <= DEFAULT_DRAIN_SECONDS]
    assert len(during) >= 2, job.renewed_at
    marks = [0.0, *during, DEFAULT_DRAIN_SECONDS]
    gaps = [later - earlier for earlier, later in zip(marks, marks[1:], strict=False)]
    assert max(gaps) < coding_jobs.LEASE_SECONDS, gaps
    assert job.settled == [pytest.approx(DEFAULT_DRAIN_SECONDS)]
    assert job.drains == 1
    assert job.closed == 1


@pytest.mark.parametrize("fails", [None, "sandbox_failed"])
def test_a_lease_lost_during_the_drain_is_not_settled(monkeypatch: pytest.MonkeyPatch, fails: str | None) -> None:
    # The claim-time renewal holds; the first renewal inside the drain finds the lease gone.
    job = _Job(monkeypatch, renewals=[True, False], fails=fails)
    outcome = job.run()
    assert outcome == {"coding_job_id": _JOB, "state": "ACTIVE", "error_code": "fence_lost"}
    assert job.settled == []
    assert job.released == []
    assert job.published == []
    assert job.stopped == []
    # The drain stopped at the lost renewal instead of waiting out the rest of its time.
    assert job.clock.now < DEFAULT_DRAIN_SECONDS
    assert job.drains == 1
    assert job.closed == 1


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _LeaseDatabase:
    def __init__(self, operations_rowcount: int) -> None:
        self.operations_rowcount = operations_rowcount
        self.statements: list[str] = []

    @contextmanager
    def transaction(self, **_kwargs: Any):
        database = self

        class _Conn:
            def execute(self, statement: Any, _params: dict[str, Any]) -> _Result:
                sql = str(statement)
                database.statements.append(sql)
                return _Result(database.operations_rowcount if sql.startswith("UPDATE operations") else 1)

        yield _Conn()


@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
def test_renew_lease_reports_whether_the_lease_was_still_held(rowcount: int, expected: bool) -> None:
    database = _LeaseDatabase(rowcount)
    renewed = coding_jobs._renew_lease(
        cast(Database, database),
        owner_id=_OWNER,
        project_id=_PROJECT,
        operation_id="op-1",
        job_id=_JOB,
        fencing_token=_FENCE,
    )
    assert renewed is expected
    assert database.statements and database.statements[0].startswith("UPDATE operations")
