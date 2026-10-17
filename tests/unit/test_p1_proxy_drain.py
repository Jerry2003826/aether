"""A coding job settles only after the model proxy drained calls still in flight when the sandbox exited."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from aether_gateway.model import ModelGateway
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker import coding_jobs
from aether_worker.coding_model_proxy import CodingModelProxy
from ledger_sql_fake import FakeLedgerDatabase

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_JOB = "00000000-0000-0000-0000-0000000000aa"
_FENCE = 7
_KEY = f"coding-{_JOB}-{_FENCE}"
_CALL = 1000


class _Harness:
    """One coding job run through _run_one with a real proxy and an in-memory ledger."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        first_call: tuple[int, int] | None,
        release_after: float | None,
        fails: BaseException | None = None,
    ) -> None:
        self.db = FakeLedgerDatabase()
        self.db.add_account(_PROJECT, _TASK)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.forward_done = threading.Event()
        self.done_at_settle: list[bool] = []
        self.first_call = first_call
        self.release_after = release_after
        self.timers: list[threading.Timer] = []
        self.fails = fails
        self.at_close: list[tuple[bool, dict[str, Any]]] = []
        harness = self
        row = {"id": _JOB, "fencing_token": _FENCE, "task_id": _TASK, "operation_id": "op-1", "base_artifact_id": "a-1"}
        monkeypatch.setattr(coding_jobs, "_claim", lambda *_a, **_k: dict(row))
        monkeypatch.setattr(coding_jobs, "_stop_confirmed", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_job_objective", lambda _row: "Make value() return 42.")
        monkeypatch.setattr(coding_jobs, "_workspace", lambda *_a, **_k: {})
        monkeypatch.setattr(coding_jobs, "_ensure_account", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_mark", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_operation", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_materialize", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_issue_coding_token", lambda *_a, **_k: "job-token-drain")
        monkeypatch.setattr(coding_jobs, "_renew_lease", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_manifest", lambda *_a, **_k: [])
        monkeypatch.setattr(coding_jobs, "_publish", lambda *_a, **_k: True)
        monkeypatch.setattr(coding_jobs, "_clear_lease", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "_fail", lambda *_a, **_k: None)
        monkeypatch.setattr(coding_jobs, "run_coding_sandbox", self.sandbox)
        real_settle = coding_jobs._settle
        self.real_settle = real_settle
        real_close = CodingModelProxy.close

        def close(proxy: CodingModelProxy) -> None:
            detail = dict(harness.db.request(_PROJECT, _TASK, _KEY)["usage_detail"])
            harness.at_close.append((harness.forward_done.is_set(), detail))
            real_close(proxy)

        monkeypatch.setattr(CodingModelProxy, "close", close)

        def settle(*args: Any, **kwargs: Any) -> None:
            harness.done_at_settle.append(harness.forward_done.is_set())
            real_settle(*args, **kwargs)

        monkeypatch.setattr(coding_jobs, "_settle", settle)
        monkeypatch.setattr(CodingModelProxy, "_forward", lambda proxy, body: harness.forward(body))
        monkeypatch.delenv("AETHER_GATEWAY_BASE", raising=False)

    def _gateway(self) -> tuple[ModelGateway, dict[str, Any]]:
        gateway = ModelGateway(cast(Database, self.db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
        gateway._coding_call_tokens = _CALL
        return gateway, {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _KEY}}

    def _open_call(self) -> None:
        """What _open_coding_ticket does to the parent: hold this call's tokens as pending."""
        detail = self.db.request(_PROJECT, _TASK, _KEY)["usage_detail"]
        detail["pending_tokens"] = int(detail.get("pending_tokens") or 0) + _CALL

    def _answer(self, incoming: int, outgoing: int) -> dict[str, Any]:
        gateway, session = self._gateway()
        result = {"id": "c", "model": "coder", "choices": [], "usage": {"prompt_tokens": incoming, "completion_tokens": outgoing}}
        gateway._add_measured_usage(session, result)
        return result

    def forward(self, _body: dict) -> dict:
        self._open_call()
        self.entered.set()
        self.release.wait(30)
        try:
            return self._answer(120, 30)
        finally:
            self.forward_done.set()

    def sandbox(self, _root: Path, *, proxy_dir: Path, job_token: str, **_kwargs: Any) -> dict[str, Any]:
        if self.first_call is not None:
            self._open_call()
            self._answer(*self.first_call)
        threading.Thread(target=_post, args=(proxy_dir / "model.sock", job_token), daemon=True).start()
        assert self.entered.wait(10), "the in-flight call never reached the proxy"
        if self.release_after is not None:
            timer = threading.Timer(self.release_after, self.release.set)
            self.timers.append(timer)
            timer.start()
        if self.fails is not None:
            raise self.fails
        # The agent process is gone; its last model call is still being forwarded.
        return {
            "exit_code": 0,
            "result": {"passed": 1, "failed": 0, "agent_completed": True, "model_requests": 1, "patch": "--- a/app.py\n+++ b/app.py\n"},
        }

    def run(self) -> dict[str, str]:
        try:
            return coding_jobs._run_one(cast(Database, self.db), cast(ArtifactStore, object()), owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB)
        finally:
            self.release.set()
            for timer in self.timers:
                timer.cancel()


def _post(path: Path, token: str) -> None:
    raw = json.dumps({"model": "coder", "messages": [{"role": "user", "content": "late"}]}).encode()
    request = (
        "POST /v1/chat/completions HTTP/1.1\r\n"
        f"authorization: Bearer {token}\r\n"
        f"content-length: {len(raw)}\r\n"
        "content-type: application/json\r\n\r\n"
    ).encode() + raw
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
        sock.sendall(request)
        sock.settimeout(40)
        while sock.recv(4096):
            pass
    except OSError:
        return
    finally:
        sock.close()


def test_late_usage_of_a_call_in_flight_when_the_sandbox_exits_is_billed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    harness = _Harness(monkeypatch, first_call=None, release_after=0.5)
    outcome = harness.run()
    assert harness.done_at_settle == [True], outcome
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    account = harness.db.account(_PROJECT, _TASK)
    assert row["usage_status"] == "known", outcome
    assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
    assert account["spent_micro_usd"] == 150
    assert account["tokens_used"] == 150
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0
    assert "unknown_usage_calls" not in row["usage_detail"]


def test_a_call_still_in_flight_after_the_drain_timeout_is_charged_at_its_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "0.3")
    harness = _Harness(monkeypatch, first_call=(100, 50), release_after=None)
    outcome = harness.run()
    assert harness.done_at_settle == [False], outcome
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    account = harness.db.account(_PROJECT, _TASK)
    assert row["usage_status"] == "known", outcome
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)
    assert row["usage_detail"]["unknown_usage_calls"] == 1
    assert row["usage_detail"]["unknown_reserved_tokens"] == _CALL
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["tokens_used"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "nan", "inf"])
def test_invalid_drain_settings_fall_back_to_the_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    from aether_worker.coding_model_proxy import DEFAULT_DRAIN_SECONDS, drain_seconds

    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", raw)
    assert drain_seconds() == DEFAULT_DRAIN_SECONDS


def test_the_drain_default_outlasts_one_forwarded_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_worker.coding_model_proxy import DEFAULT_DRAIN_SECONDS, drain_seconds

    assert 30 < DEFAULT_DRAIN_SECONDS <= 120
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "2.5")
    assert drain_seconds() == 2.5


def _settle_later(harness: _Harness) -> None:
    """Whatever settles the abandoned reservation later reads what the job left on the row."""
    assert harness.forward_done.wait(10)
    harness.real_settle(
        coding_jobs.BudgetLedger(cast(Database, harness.db)),
        owner_id=_OWNER,
        project_id=_PROJECT,
        task_id=_TASK,
        request_key=_KEY,
        reserved=True,
        result=None,
    )


def test_an_unexpected_sandbox_error_still_waits_for_a_call_in_flight_before_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    harness = _Harness(monkeypatch, first_call=None, release_after=0.5, fails=RuntimeError("sandbox broke"))
    with pytest.raises(RuntimeError, match="sandbox broke"):
        harness.run()
    assert len(harness.at_close) == 1
    finished, detail = harness.at_close[0]
    assert finished is True
    assert (detail["measured_input_tokens"], detail["measured_output_tokens"]) == (120, 30)
    assert detail["pending_tokens"] == 0
    _settle_later(harness)
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    account = harness.db.account(_PROJECT, _TASK)
    assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
    assert account["spent_micro_usd"] == 150
    assert account["tokens_used"] == 150
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_an_unexpected_sandbox_error_charges_a_call_outlasting_the_drain_at_its_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "0.3")
    harness = _Harness(monkeypatch, first_call=(100, 50), release_after=None, fails=RuntimeError("sandbox broke"))
    with pytest.raises(RuntimeError, match="sandbox broke"):
        harness.run()
    assert len(harness.at_close) == 1
    finished, detail = harness.at_close[0]
    assert finished is False
    assert detail["unknown_usage_calls"] == 1
    assert detail["unknown_reserved_tokens"] == _CALL
    assert detail["pending_tokens"] == 0
    _settle_later(harness)
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    account = harness.db.account(_PROJECT, _TASK)
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["tokens_used"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


@pytest.mark.parametrize("fails", [None, "sandbox_error"])
def test_the_proxy_is_drained_once_per_job(monkeypatch: pytest.MonkeyPatch, fails: str | None) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    error = coding_jobs.CodingSandboxError("sandbox_failed") if fails else None
    harness = _Harness(monkeypatch, first_call=None, release_after=0.2, fails=error)
    drains: list[int] = []
    real_drain = CodingModelProxy.drain

    def drain(proxy: CodingModelProxy, **kwargs: Any) -> int:
        drains.append(1)
        return real_drain(proxy, **kwargs)

    monkeypatch.setattr(CodingModelProxy, "drain", drain)
    monkeypatch.setattr(coding_jobs, "_sandbox_stop", lambda *_a, **_k: {"coding_job_id": _JOB, "state": "FAILED"})
    harness.run()
    assert drains == [1]
    assert len(harness.at_close) == 1
    assert harness.at_close[0][0] is True
