"""A coding attempt whose calls were all definitely not sent, or not billable, gives its hold back.

When nothing reached the provider (model_unavailable, a refused connection with sent=False) or the
only reply was a complete non-2xx error (not billed), the gateway leaves no measured, unknown or
pending tokens on the parent reservation. Settlement must then release the reservation in full and
say why (released_unsent), instead of marking the whole hold unknown and stranding it. Any sign that
a call may have been billed (unknown_reserved_tokens, pending tokens, usage_missing, a cut error
body, measured usage) keeps the old booking.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.budget import BudgetLedger
from aether_storage.db import Database
from aether_worker import coding_jobs
from ledger_sql_fake import FakeLedgerDatabase

_OWNER = "owner-1"
_PROJECT = "p"
_TASK = "t"
_JOB = "00000000-0000-0000-0000-0000000000cd"
_FENCE = 5
_KEY = f"coding-{_JOB}-{_FENCE}"
_CHILD = f"{_KEY}:call:1"
_HOLD = 4096
_CALL = 1200


def _db(**detail: Any) -> FakeLedgerDatabase:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=_HOLD, tokens_held=_HOLD, requests_admitted=1)
    base = {"reserved_tokens": _HOLD, "call_reserve_tokens": _HOLD, "call_reserve_micro_usd": _HOLD}
    base.update(detail)
    db.add_request(_PROJECT, _TASK, _KEY, reserved_micro_usd=_HOLD, usage_detail=base)
    return db


def _gateway(db: FakeLedgerDatabase) -> ModelGateway:
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    gateway._coding_call_tokens = _CALL
    gateway._coding_child_key = _CHILD
    return gateway


def _call_fails(db: FakeLedgerDatabase, denied: GatewayDenied) -> None:
    """One call: its ticket and pending hold are opened, then the gateway records how it failed."""
    db.add_request(_PROJECT, _TASK, _CHILD, role="coding_call", usage_detail={})
    db.request(_PROJECT, _TASK, _KEY)["usage_detail"]["pending_tokens"] = _CALL
    session = {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _KEY}}
    _gateway(db)._note_upstream_failure(session, denied, release_pending=True)


def _settle(db: FakeLedgerDatabase) -> None:
    coding_jobs._settle(
        BudgetLedger(cast(Database, db)),
        owner_id=_OWNER,
        project_id=_PROJECT,
        task_id=_TASK,
        request_key=_KEY,
        reserved=True,
        result=None,
        fence=(_JOB, _FENCE),
    )


def _assert_released(db: FakeLedgerDatabase, reason: str) -> None:
    row = db.request(_PROJECT, _TASK, _KEY)
    account = db.account(_PROJECT, _TASK)
    assert row["usage_status"] == "cancelled_before_dispatch", row
    assert row["actual_micro_usd"] is None
    assert row["usage_detail"]["released_unsent"] == reason
    assert account["held_micro_usd"] == 0
    assert account["tokens_held"] == 0
    assert account["spent_micro_usd"] == 0
    assert account["tokens_used"] == 0


def test_model_unavailable_releases_the_whole_hold() -> None:
    db = _db(admission_error="model_unavailable")
    _settle(db)
    _assert_released(db, "model_unavailable")


def test_a_call_refused_before_it_was_sent_releases_the_hold() -> None:
    db = _db()
    _call_fails(db, GatewayDenied("upstream_unreachable", sent=False))
    _settle(db)
    _assert_released(db, "upstream_unreachable")


def test_a_complete_non_2xx_reply_is_not_billed_and_releases_the_hold() -> None:
    db = _db()
    _call_fails(db, GatewayDenied("upstream_http_429", sent=True, upstream_status=429, provider_error="rate_limited"))
    detail = db.request(_PROJECT, _TASK, _KEY)["usage_detail"]
    assert "unknown_reserved_tokens" not in detail
    _settle(db)
    _assert_released(db, "upstream_http_429")


def test_an_attempt_that_never_called_the_model_releases_the_hold() -> None:
    db = _db()
    _settle(db)
    _assert_released(db, "no_model_call")


@pytest.mark.parametrize(
    "denied",
    [
        GatewayDenied("upstream_timeout", sent=True),
        GatewayDenied("upstream_error", sent=True, upstream_status=500, error_body_truncated=True),
    ],
    ids=["sent_without_answer", "cut_error_body"],
)
def test_a_call_that_may_have_been_billed_keeps_the_charge(denied: GatewayDenied) -> None:
    db = _db()
    _call_fails(db, denied)
    _settle(db)
    row = db.request(_PROJECT, _TASK, _KEY)
    assert row["usage_status"] != "cancelled_before_dispatch"
    assert "released_unsent" not in row["usage_detail"]
    assert db.account(_PROJECT, _TASK)["held_micro_usd"] + db.account(_PROJECT, _TASK)["spent_micro_usd"] > 0


@pytest.mark.parametrize(
    "detail",
    [
        {"pending_tokens": _CALL},
        {"usage_missing": True},
        {"unknown_reserved_tokens": _CALL, "unknown_usage_calls": 1},
        {"proxy_drained": True, "drain_unfinished": 1},
    ],
    ids=["pending", "usage_missing", "unknown_reserved", "drain_gave_up_on_a_call"],
)
def test_any_sign_of_a_billable_call_is_not_released(detail: dict[str, Any]) -> None:
    db = _db(**detail)
    _settle(db)
    row = db.request(_PROJECT, _TASK, _KEY)
    assert row["usage_status"] != "cancelled_before_dispatch"
    assert "released_unsent" not in row["usage_detail"]
