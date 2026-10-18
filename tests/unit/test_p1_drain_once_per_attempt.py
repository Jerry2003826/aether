"""Guard: the model proxy is drained only at the end of an attempt, and each attempt has its own.

Draining charges every call still pending at its reservation and marks the reservation drained,
so a drain while the sandbox can still call, or a proxy (and its job token) carried into a later
attempt, would book one attempt's calls on another attempt's row (ADR-F46 R4).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker import coding_jobs

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_JOB = "00000000-0000-0000-0000-0000000000cc"


class _Attempts:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fencings: list[int], fails: str | None = None) -> None:
        self.events: list[tuple[str, int, str]] = []
        self.proxies: list[Any] = []
        self.running: Any = None
        self.fencings = list(fencings)
        self.fails = fails
        recorder = self

        class _Proxy:
            def __init__(self, _database: Any, *, job_token: str) -> None:
                self.job_token = job_token
                self.directory = Path(f"/nonexistent/proxy-{len(recorder.proxies)}")
                self.revoked_in_band = False
                self.closed = False
                self.number = len(recorder.proxies)
                recorder.proxies.append(self)
                recorder.events.append(("open", self.number, job_token))

            def revoke(self) -> None:
                recorder.events.append(("revoke", self.number, self.job_token))

            def drain(self, **_kwargs: Any) -> int:
                assert not self.closed, "a closed proxy was drained"
                assert recorder.running is None, "drained while the sandbox could still call the model"
                recorder.events.append(("drain", self.number, self.job_token))
                return 0

            def close(self) -> None:
                self.closed = True
                recorder.events.append(("close", self.number, self.job_token))

        def claim(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            fencing = recorder.fencings.pop(0)
            return {"id": _JOB, "fencing_token": fencing, "task_id": _TASK, "operation_id": "op-1", "base_artifact_id": "a-1"}

        monkeypatch.setattr(coding_jobs, "CodingModelProxy", _Proxy)
        monkeypatch.setattr(coding_jobs, "BudgetLedger", lambda database: SimpleNamespace(database=database, reserve=lambda **_k: True))
        monkeypatch.setattr(coding_jobs, "_claim", claim)
        for name in ("_settle_prior_attempts", "_stop_confirmed", "_ensure_account", "_mark", "_operation", "_materialize"):
            monkeypatch.setattr(coding_jobs, name, lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_job_objective", lambda _row: "Make value() return 42.")
        monkeypatch.setattr(coding_jobs, "_workspace", lambda *_a, **_k: {})
        monkeypatch.setattr(coding_jobs, "_issue_coding_token", lambda *_a, attempt_id, **_k: f"job-token-attempt-{attempt_id}")
        monkeypatch.setattr(coding_jobs, "_renew_lease", lambda *_a, **_k: True)
        monkeypatch.setattr(coding_jobs, "_manifest", lambda *_a, **_k: [])
        monkeypatch.setattr(coding_jobs, "_request_detail", lambda *_a, **_k: {})
        monkeypatch.setattr(coding_jobs, "_successful_model_requests", lambda *_a, **_k: 1)
        monkeypatch.setattr(coding_jobs, "_coding_terminal", lambda *_a, **_k: ("FINISHED", None))
        monkeypatch.setattr(coding_jobs, "_publish", lambda *_a, **_k: True)
        monkeypatch.setattr(coding_jobs, "_settle", lambda *_a, request_key, **_k: recorder.events.append(("settle", -1, request_key)))
        monkeypatch.setattr(coding_jobs, "_release_hold", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_clear_lease", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_fail", lambda *_a, **_k: None)
        monkeypatch.setattr(
            coding_jobs, "_sandbox_stop", lambda *_a, **_k: {"coding_job_id": _JOB, "state": "FAILED", "error_code": "sandbox_failed"}
        )
        monkeypatch.setattr(coding_jobs, "run_coding_sandbox", self.sandbox)

    def sandbox(self, _root: Path, *, proxy_dir: Path, job_token: str, **_kwargs: Any) -> dict[str, Any]:
        proxy = self.proxies[-1]
        assert proxy.directory == proxy_dir and proxy.job_token == job_token
        self.running = proxy
        self.events.append(("sandbox", proxy.number, job_token))
        try:
            if self.fails is not None:
                raise coding_jobs.CodingSandboxError(self.fails)
            return {"exit_code": 0, "result": {"passed": 1, "failed": 0, "agent_completed": True, "patch": ""}}
        finally:
            self.events.append(("sandbox_done", proxy.number, job_token))
            self.running = None

    def run(self) -> dict[str, str]:
        return coding_jobs._run_one(cast(Database, object()), cast(ArtifactStore, object()), owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB)


@pytest.mark.parametrize("fails", [None, "sandbox_failed"])
def test_drain_runs_once_at_the_end_of_the_attempt(monkeypatch: pytest.MonkeyPatch, fails: str | None) -> None:
    attempts = _Attempts(monkeypatch, fencings=[9], fails=fails)
    attempts.run()

    kinds = [kind for kind, _number, _token in attempts.events if kind != "settle"]
    assert kinds == ["open", "sandbox", "sandbox_done", "revoke", "drain", "close"]


def test_a_later_attempt_never_reuses_the_earlier_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = _Attempts(monkeypatch, fencings=[9, 10])
    attempts.run()
    attempts.run()

    first, second = attempts.proxies
    assert first is not second
    assert first.job_token == "job-token-attempt-9"
    assert second.job_token == "job-token-attempt-10"
    # Every event of the first proxy, its drain and close included, happens before the second opens.
    opened_second = attempts.events.index(("open", 1, "job-token-attempt-10"))
    assert all(number != 0 for _kind, number, _token in attempts.events[opened_second:])
    assert [kind for kind, number, _token in attempts.events if number == 0] == ["open", "sandbox", "sandbox_done", "revoke", "drain", "close"]
    assert [kind for kind, number, _token in attempts.events if number == 1] == ["open", "sandbox", "sandbox_done", "revoke", "drain", "close"]
    # Each attempt settles its own reservation row once, after its own drain.
    settled = [token for kind, _number, token in attempts.events if kind == "settle"]
    assert settled == [coding_jobs.attempt_request_key(_JOB, 9), coding_jobs.attempt_request_key(_JOB, 10)]
    assert attempts.events.index(("settle", -1, settled[0])) > attempts.events.index(("drain", 0, "job-token-attempt-9"))
    assert attempts.events.index(("settle", -1, settled[1])) > attempts.events.index(("drain", 1, "job-token-attempt-10"))
