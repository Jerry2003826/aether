"""A coding attempt's money is its tokens at the configured rate, booked apart from the tokens.

The rate is the one recorded on the reservation at reserve time:
call_reserve_micro_usd / call_reserve_tokens (AETHER_CODING_CALL_RESERVE_MICRO_USD and
AETHER_CODING_CALL_RESERVE_TOKENS). Measured usage used to be booked at 1 micro-USD per token
whatever the rate, and calls without usage at the hold's own average. Tokens never change with
the rate; only spent_micro_usd does. An overrun is checked money against money and tokens against
tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.artifacts import ArtifactStore
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker import coding_jobs
from aether_worker.coding_model_proxy import CodingModelProxy
from ledger_sql_fake import FakeLedgerDatabase, prices_at

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_JOB = "00000000-0000-0000-0000-0000000000ab"
_FENCE = 3
_KEY = f"coding-{_JOB}-{_FENCE}"
_PARENT = "coding-job-rate"
_CALL = 600


def _session(key: str) -> dict[str, Any]:
    return {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": key}}


def _gateway(db: FakeLedgerDatabase, call_tokens: int = _CALL) -> ModelGateway:
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    gateway._coding_call_tokens = call_tokens
    return gateway


def _open_call(db: FakeLedgerDatabase, key: str, call_tokens: int = _CALL) -> None:
    """What _open_coding_ticket does to the parent: hold this call's tokens as pending."""
    detail = db.request(_PROJECT, _TASK, key)["usage_detail"]
    detail["pending_tokens"] = int(detail.get("pending_tokens") or 0) + call_tokens


def _run_job(monkeypatch: pytest.MonkeyPatch, *, reserve_tokens: int, reserve_micro: int) -> FakeLedgerDatabase:
    """One coding job through _run_one under the given reserve configuration; its one call reports 300 in / 200 out."""
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_TOKENS", str(reserve_tokens))
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", str(reserve_micro))
    # Measured tokens are billed at the configured prices (ADR-F49); here they equal the reserve rate.
    per_mtok = str(reserve_micro * 1_000_000 // reserve_tokens)
    monkeypatch.setenv("AETHER_CODING_INPUT_MICRO_USD_PER_MTOK", per_mtok)
    monkeypatch.setenv("AETHER_CODING_OUTPUT_MICRO_USD_PER_MTOK", per_mtok)
    monkeypatch.delenv("AETHER_GATEWAY_BASE", raising=False)
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK)
    row = {"id": _JOB, "fencing_token": _FENCE, "task_id": _TASK, "operation_id": "op-1", "base_artifact_id": "a-1"}
    for name, value in {
        "_claim": lambda *_a, **_k: dict(row),
        "_stop_confirmed": lambda *_a, **_k: None,
        "_job_objective": lambda _row: "Make value() return 42.",
        "_workspace": lambda *_a, **_k: {},
        "_ensure_account": lambda *_a, **_k: None,
        "_mark": lambda *_a, **_k: None,
        "_operation": lambda *_a, **_k: None,
        "_materialize": lambda *_a, **_k: None,
        "_issue_coding_token": lambda *_a, **_k: "job-token-rate",
        "_renew_lease": lambda *_a, **_k: True,
        "_manifest": lambda *_a, **_k: [],
        "_publish": lambda *_a, **_k: True,
        "_clear_lease": lambda *_a, **_k: None,
        "_fail": lambda *_a, **_k: None,
    }.items():
        monkeypatch.setattr(coding_jobs, name, value)

    def sandbox(_root: Path, **_kwargs: Any) -> dict[str, Any]:
        _open_call(db, _KEY)
        _gateway(db)._add_measured_usage(
            _session(_KEY), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 300, "completion_tokens": 200}}
        )
        return {
            "exit_code": 0,
            "result": {"passed": 1, "failed": 0, "agent_completed": True, "model_requests": 1, "patch": "--- a/app.py\n+++ b/app.py\n"},
        }

    monkeypatch.setattr(coding_jobs, "run_coding_sandbox", sandbox)
    monkeypatch.setattr(CodingModelProxy, "_forward", lambda _proxy, _body: {})
    coding_jobs._run_one(cast(Database, db), cast(ArtifactStore, object()), owner_id=_OWNER, project_id=_PROJECT, job_id=_JOB)
    return db


def test_settled_money_follows_the_configured_rate_and_tokens_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap = _run_job(monkeypatch, reserve_tokens=1000, reserve_micro=1000)
    dear = _run_job(monkeypatch, reserve_tokens=1000, reserve_micro=7000)
    cheap_account, dear_account = cheap.account(_PROJECT, _TASK), dear.account(_PROJECT, _TASK)
    cheap_row, dear_row = cheap.request(_PROJECT, _TASK, _KEY), dear.request(_PROJECT, _TASK, _KEY)
    assert cheap_row["usage_status"] == dear_row["usage_status"] == "known"
    assert cheap_account["spent_micro_usd"] == 500
    assert dear_account["spent_micro_usd"] == 3500
    assert cheap_row["actual_micro_usd"] == 500
    assert dear_row["actual_micro_usd"] == 3500
    assert cheap_account["tokens_used"] == dear_account["tokens_used"] == 500
    assert (cheap_row["input_tokens"], cheap_row["output_tokens"]) == (300, 200)
    assert (dear_row["input_tokens"], dear_row["output_tokens"]) == (300, 200)
    for account in (cheap_account, dear_account):
        assert account["held_micro_usd"] == 0
        assert account["tokens_held"] == 0
    assert dear_row["usage_detail"]["book_result"] == "settled"


def _held_row(*, reserved_tokens: int, reserved_micro: int, rate_tokens: int, rate_micro: int, **detail: Any) -> FakeLedgerDatabase:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=reserved_micro, tokens_held=reserved_tokens, requests_admitted=1)
    db.add_request(
        _PROJECT,
        _TASK,
        _PARENT,
        reserved_micro_usd=reserved_micro,
        usage_detail={
            "reserved_tokens": reserved_tokens,
            "call_reserve_tokens": rate_tokens,
            "call_reserve_micro_usd": rate_micro,
            **prices_at(rate_micro, rate_tokens),
            **detail,
        },
    )
    return db


def _settle(db: FakeLedgerDatabase) -> None:
    coding_jobs._settle(
        BudgetLedger(cast(Database, db)), owner_id=_OWNER, project_id=_PROJECT, task_id=_TASK, request_key=_PARENT, reserved=True, result=None
    )


def test_unknown_calls_are_charged_at_the_configured_rate_not_the_hold_average() -> None:
    # Rate 1/3 micro-USD per token. Every top-up rounds up, so after 600 one-token top-ups of 1 micro-USD
    # each the hold is 3600 tokens for 1600 micro-USD: an average of 4/9, not 1/3.
    db = _held_row(
        reserved_tokens=3600,
        reserved_micro=1600,
        rate_tokens=3000,
        rate_micro=1000,
        reservation_extensions=600,
        extended_tokens=600,
        extended_micro_usd=600,
    )
    measured = _gateway(db, call_tokens=30)
    _open_call(db, _PARENT, 30)
    measured._add_measured_usage(_session(_PARENT), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 10}})
    timed_out = _gateway(db, call_tokens=3000)
    _open_call(db, _PARENT, 3000)
    timed_out._note_upstream_failure(_session(_PARENT), GatewayDenied("upstream_error", sent=True), release_pending=True)

    _settle(db)

    account = db.account(_PROJECT, _TASK)
    row = db.request(_PROJECT, _TASK, _PARENT)
    # 30 measured tokens cost ceil(30 / 3) = 10; the 3000-token unknown call costs ceil(3000 / 3) = 1000.
    assert account["spent_micro_usd"] == 10 + 1000
    assert row["actual_micro_usd"] == 10 + 1000
    assert row["usage_detail"]["unknown_charged_micro_usd"] == 1000
    assert row["usage_detail"]["unknown_charged_tokens"] == 3000
    assert account["tokens_used"] == 30 + 3000
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_overrun_compares_money_with_money_and_tokens_with_tokens() -> None:
    # Rate 7 micro-USD per token; 1000 tokens held for 7000 micro-USD. 1200 measured tokens cost 8400.
    db = _held_row(reserved_tokens=1000, reserved_micro=7000, rate_tokens=1000, rate_micro=7000)
    gateway = _gateway(db, call_tokens=1000)
    _open_call(db, _PARENT, 1000)
    gateway._add_measured_usage(_session(_PARENT), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 200}})

    _settle(db)

    account = db.account(_PROJECT, _TASK)
    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    assert detail["book_result"] == "overrun"
    assert detail["overrun_micro_usd"] == 8400 - 7000
    assert detail["overrun_tokens"] == 1200 - 1000
    assert account["spent_micro_usd"] == 8400
    assert account["tokens_used"] == 1200
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0


def test_usage_within_both_holds_is_not_an_overrun_at_a_rate_above_one() -> None:
    # 900 tokens at 7 micro-USD per token is 6300 micro-USD: inside the 7000 held. Read 1:1 it would be 900.
    db = _held_row(reserved_tokens=1000, reserved_micro=7000, rate_tokens=1000, rate_micro=7000)
    gateway = _gateway(db, call_tokens=1000)
    _open_call(db, _PARENT, 1000)
    gateway._add_measured_usage(_session(_PARENT), {"model": "coder", "choices": [], "usage": {"prompt_tokens": 700, "completion_tokens": 200}})

    _settle(db)

    detail = db.request(_PROJECT, _TASK, _PARENT)["usage_detail"]
    assert detail["book_result"] == "settled"
    assert "overrun_micro_usd" not in detail
    assert db.account(_PROJECT, _TASK)["spent_micro_usd"] == 6300
