"""Coding calls are billed at configured per-token prices, input and output apart, never 1 micro-USD per token.

A live run settled spent_micro_usd equal to tokens_used (21502/21502): the reservation's rate was the
4096/4096 accounting default and model_requests.pricing_version stayed empty. These tests pin the
fix: the reservation records an input and an output price with their pricing_version, every measured
call and the settlement use them, and a missing price or version is an explicit error.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Any, cast

import pytest
from aether_gateway.model import ModelGateway
from aether_storage import budget
from aether_storage.budget import BudgetError
from aether_storage.db import Database
from ledger_sql_fake import FakeLedgerDatabase
from test_p1_proxy_drain import _KEY, _PROJECT, _TASK, _Harness

_IN = "AETHER_CODING_INPUT_MICRO_USD_PER_MTOK"
_OUT = "AETHER_CODING_OUTPUT_MICRO_USD_PER_MTOK"
# 0.28 and 0.42 USD per million tokens, in micro-USD per million tokens.
_PRICED = {"pricing_version": "config-280000-420000", "input_micro_usd_per_mtok": 280_000, "output_micro_usd_per_mtok": 420_000}


@pytest.fixture(autouse=True)
def _no_ambient_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (_IN, _OUT, "AETHER_LIVE_MODEL_ALIAS", "AETHER_CODING_CALL_RESERVE_MICRO_USD", "AETHER_CODING_CALL_RESERVE_TOKENS"):
        monkeypatch.delenv(name, raising=False)


def _usage_micro_usd(incoming: int, outgoing: int, detail: dict[str, Any]) -> int:
    price = getattr(budget, "coding_usage_micro_usd", None)
    assert price is not None, "aether_storage.budget has no coding_usage_micro_usd(input, output, detail)"
    return price(incoming, outgoing, detail)


def test_input_and_output_tokens_are_priced_at_their_own_configured_rates() -> None:
    # The live run's tokens: 20800 in, 702 out. 20800 * 0.28 + 702 * 0.42 = 6118.84, rounded up.
    assert _usage_micro_usd(20_800, 702, dict(_PRICED)) == 6119
    assert _usage_micro_usd(20_800, 702, dict(_PRICED)) != 20_800 + 702
    # Same token total, split the other way: a different amount, so the two rates are applied apart.
    assert _usage_micro_usd(702, 20_800, dict(_PRICED)) == 8933
    assert _usage_micro_usd(0, 0, dict(_PRICED)) == 0


@pytest.mark.parametrize(
    "detail",
    [
        {},
        {**_PRICED, "pricing_version": ""},
        {**_PRICED, "pricing_version": None},
        {key: value for key, value in _PRICED.items() if key != "input_micro_usd_per_mtok"},
        {key: value for key, value in _PRICED.items() if key != "output_micro_usd_per_mtok"},
        {**_PRICED, "output_micro_usd_per_mtok": "420000"},
        {**_PRICED, "input_micro_usd_per_mtok": -1},
    ],
    ids=["nothing", "empty-version", "null-version", "no-input-price", "no-output-price", "price-not-int", "negative-price"],
)
def test_a_missing_version_or_price_is_an_error_not_one_micro_usd_per_token(detail: dict[str, Any]) -> None:
    with pytest.raises(BudgetError) as caught:
        _usage_micro_usd(120, 30, detail)
    assert caught.value.code == "pricing_missing"


def test_the_per_call_rate_fallback_no_longer_prices_one_micro_usd_per_token() -> None:
    # budget.py used to return the token count for a row without call_reserve_tokens/_micro_usd.
    with pytest.raises(BudgetError) as caught:
        budget.coding_call_micro_usd(100, {})
    assert caught.value.code == "coding_rate_missing"
    assert budget.coding_call_micro_usd(100, {"call_reserve_tokens": 4096, "call_reserve_micro_usd": 1721}) == 43


def _gateway_row(detail: dict[str, Any]) -> tuple[FakeLedgerDatabase, ModelGateway, dict[str, Any]]:
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK)
    db.add_request(
        _PROJECT,
        _TASK,
        _KEY,
        reserved_micro_usd=40_960,
        usage_detail={"reserved_tokens": 4096, "call_reserve_tokens": 4096, "call_reserve_micro_usd": 40_960, "pending_tokens": 1000, **detail},
    )
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    gateway._coding_call_tokens = 1000
    return db, gateway, {"project_id": _PROJECT, "task_id": _TASK, "scope": {"request_key": _KEY}}


def _answer(gateway: ModelGateway, session: dict[str, Any], incoming: int, outgoing: int) -> None:
    result = {"id": "c", "model": "coder", "choices": [], "usage": {"prompt_tokens": incoming, "completion_tokens": outgoing}}
    gateway._add_measured_usage(session, result)


def test_the_gateway_prices_each_measured_call_by_its_input_and_output_tokens() -> None:
    db, gateway, session = _gateway_row({"pricing_version": "config-2000000-10000000", "input_micro_usd_per_mtok": 2_000_000, "output_micro_usd_per_mtok": 10_000_000})
    _answer(gateway, session, 120, 30)
    detail = db.request(_PROJECT, _TASK, _KEY)["usage_detail"]
    # 120 * 2 + 30 * 10 = 540, not 150 (one per token) and not the 4096-token reservation rate.
    assert detail["measured_micro_usd"] == 540
    assert detail["measured_priced_tokens"] == 150


def test_the_gateway_refuses_to_price_a_measured_call_without_prices() -> None:
    _db, gateway, session = _gateway_row({})
    with pytest.raises(BudgetError) as caught:
        _answer(gateway, session, 120, 30)
    assert caught.value.code == "pricing_missing"


def test_a_job_settles_at_the_configured_prices_and_records_the_pricing_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_IN, "2000000")
    monkeypatch.setenv(_OUT, "10000000")
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    harness = _Harness(monkeypatch, first_call=None, release_after=0.5)
    outcome = harness.run()
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    account = harness.db.account(_PROJECT, _TASK)
    assert row["usage_status"] == "known", outcome
    assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
    assert account["tokens_used"] == 150
    assert account["spent_micro_usd"] == 540
    assert account["spent_micro_usd"] != account["tokens_used"]
    assert row["pricing_version"] == "config-2000000-10000000"
    detail = row["usage_detail"]
    assert (detail["input_micro_usd_per_mtok"], detail["output_micro_usd_per_mtok"]) == (2_000_000, 10_000_000)
    # The per-call hold follows the prices too (4096 tokens at the higher rate), not 4096 micro-USD.
    assert detail["call_reserve_micro_usd"] == 40_960


def test_the_sandbox_total_fallback_is_priced_by_input_and_output_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_storage.budget import BudgetLedger
    from aether_worker import coding_jobs

    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK)
    db.add_request(
        _PROJECT,
        _TASK,
        _KEY,
        reserved_micro_usd=40_960,
        usage_detail={"reserved_tokens": 4096, "call_reserve_tokens": 4096, "call_reserve_micro_usd": 40_960, **_PRICED},
    )
    ledger = BudgetLedger(cast(Database, db))
    result = {"input_tokens": 20_800, "output_tokens": 702}
    coding_jobs._settle(ledger, owner_id="o", project_id=_PROJECT, task_id=_TASK, request_key=_KEY, reserved=True, result=result)
    account = db.account(_PROJECT, _TASK)
    assert account["tokens_used"] == 21_502
    assert account["spent_micro_usd"] == 6119
    assert db.request(_PROJECT, _TASK, _KEY)["pricing_version"] == _PRICED["pricing_version"]


def test_the_live_alias_is_priced_from_the_litellm_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_LIVE_MODEL_ALIAS", "deepseek-chat")
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    harness = _Harness(monkeypatch, first_call=None, release_after=0.5)
    outcome = harness.run()
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    detail = row["usage_detail"]
    assert row["pricing_version"] == f"litellm-{version('litellm')}", outcome
    assert (detail["input_micro_usd_per_mtok"], detail["output_micro_usd_per_mtok"]) == (280_000, 420_000)
    # 120 * 0.28 + 30 * 0.42 = 46.2, rounded up.
    assert harness.db.account(_PROJECT, _TASK)["spent_micro_usd"] == 47


_UNPRICED_REPORT = {"exit_code": 0, "result": {"passed": 1, "failed": 0, "agent_completed": True, "model_requests": 0, "patch": ""}}


@pytest.mark.parametrize(
    "env",
    [{}, {_IN: "2000000"}, {_OUT: "10000000"}, {_IN: "abc", _OUT: "10000000"}, {_IN: "-5", _OUT: "10000000"}],
    ids=["no-price-anywhere", "input-only", "output-only", "not-a-number", "negative"],
)
def test_a_job_without_a_usable_price_fails_before_any_model_call(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    harness = _Harness(monkeypatch, first_call=None, release_after=0.5)
    called: list[bool] = []
    monkeypatch.setattr("aether_worker.coding_jobs.run_coding_sandbox", lambda *_a, **_k: called.append(True) or dict(_UNPRICED_REPORT))
    outcome = harness.run()
    assert outcome["state"] == "FAILED", outcome
    assert outcome["error_code"] == "pricing_missing"
    assert called == []
    assert (_PROJECT, _TASK, _KEY) not in harness.db.requests
    assert harness.db.account(_PROJECT, _TASK)["held_micro_usd"] == 0


@pytest.mark.parametrize(
    ("detail", "code"),
    [
        ({"call_reserve_tokens": 4096, "call_reserve_micro_usd": 4096}, "pricing_missing"),
        ({"call_reserve_tokens": 4096, "call_reserve_micro_usd": 4096, **_PRICED, "pricing_version": ""}, "pricing_missing"),
        (dict(_PRICED), "coding_rate_missing"),
    ],
    ids=["no-prices", "empty-version", "no-per-call-rate"],
)
def test_admission_refuses_a_row_it_cannot_price(monkeypatch: pytest.MonkeyPatch, detail: dict[str, Any], code: str) -> None:
    import aether_gateway.model as gateway_model
    from aether_gateway.model import GatewayDenied

    monkeypatch.setattr(gateway_model, "_coding_lease_held", lambda _conn, **_fence: True)
    db = FakeLedgerDatabase()
    db.add_account(_PROJECT, _TASK, held_micro_usd=8192, tokens_held=8192, requests_admitted=1)
    db.add_request(_PROJECT, _TASK, _KEY, reserved_micro_usd=8192, usage_detail={"reserved_tokens": 8192, **detail})
    gateway = ModelGateway(cast(Database, db), upstream_base="http://127.0.0.1:9/v1", provider_key="unit-key")
    with pytest.raises(GatewayDenied) as caught:
        gateway._open_coding_ticket(
            project_id=_PROJECT,
            task_id=_TASK,
            parent_key=_KEY,
            child_key=f"{_KEY}:call:{1:064x}",
            attempt_id="7",
            alias="coder",
            prompt_sha=f"{1:064x}",
            job_id="00000000-0000-0000-0000-000000000001",
            operation_id="00000000-0000-0000-0000-000000000002",
            fencing=7,
            call_tokens=1000,
        )
    assert caught.value.code == code
    assert int(db.request(_PROJECT, _TASK, _KEY)["usage_detail"].get("pending_tokens") or 0) == 0


def _answer_with(harness: _Harness, incoming: int, outgoing: int, reply_model: str) -> None:
    """Make the harness's one in-flight call answer with this usage and this reply `model`."""

    def forward(_body: dict) -> dict:
        harness._open_call()
        harness.entered.set()
        harness.release.wait(30)
        try:
            gateway, session = harness._gateway()
            result = {"id": "c", "model": reply_model, "choices": [], "usage": {"prompt_tokens": incoming, "completion_tokens": outgoing}}
            gateway._add_measured_usage(session, result)
            return result
        finally:
            harness.forward_done.set()

    harness.forward = forward  # type: ignore[method-assign]


def test_the_per_call_hold_uses_the_higher_price_so_an_output_heavy_call_settles_within_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Input cheap, output 36 times dearer. The hold per call is the reserved tokens at the higher price:
    # 4096 * 10 = 40960. A call of 10 in / 4000 out costs 10 * 0.28 + 4000 * 10 = 40002.8 -> 40003, inside it.
    # Holding at the input price (4096 * 0.28 = 1147) would make this ordinary call an overrun.
    monkeypatch.setenv(_IN, "280000")
    monkeypatch.setenv(_OUT, "10000000")
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    harness = _Harness(monkeypatch, first_call=None, release_after=0.5)
    _answer_with(harness, 10, 4000, "coder")
    outcome = harness.run()
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    detail = row["usage_detail"]
    account = harness.db.account(_PROJECT, _TASK)
    assert detail["call_reserve_micro_usd"] == 4096 * 10 == row["reserved_micro_usd"], outcome
    assert row["usage_status"] == "known"
    assert account["spent_micro_usd"] == 40_003
    assert account["spent_micro_usd"] <= row["reserved_micro_usd"]
    assert detail["book_result"] == "settled"
    assert "overrun_micro_usd" not in detail
    assert (account["held_micro_usd"], account["tokens_held"]) == (0, 0)


def test_the_hold_takes_the_higher_price_whichever_side_it_is_on() -> None:
    from aether_worker.coding_jobs import coding_call_reserve_micro_usd

    output_dear = {"pricing_version": "v", "input_micro_usd_per_mtok": 280_000, "output_micro_usd_per_mtok": 10_000_000}
    input_dear = {"pricing_version": "v", "input_micro_usd_per_mtok": 10_000_000, "output_micro_usd_per_mtok": 280_000}
    assert coding_call_reserve_micro_usd(output_dear) == coding_call_reserve_micro_usd(input_dear) == 4096 * 10


def test_prices_follow_the_request_alias_not_the_model_the_reply_names(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    # The provider answers naming another model, priced differently in the same table (ADR-F46: the
    # reply's model is recorded as the revision only). The bill stays at the request alias's prices.
    assert (litellm.model_cost["gpt-4o"]["input_cost_per_token"], litellm.model_cost["gpt-4o"]["output_cost_per_token"]) != (2.8e-07, 4.2e-07)
    monkeypatch.setenv("AETHER_LIVE_MODEL_ALIAS", "deepseek-chat")
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    harness = _Harness(monkeypatch, first_call=None, release_after=0.5)
    _answer_with(harness, 120, 30, "gpt-4o")
    outcome = harness.run()
    row = harness.db.request(_PROJECT, _TASK, _KEY)
    detail = row["usage_detail"]
    assert row["model_revision"] == "gpt-4o", outcome
    assert detail["pricing_model"] == "deepseek-chat"
    assert (detail["input_micro_usd_per_mtok"], detail["output_micro_usd_per_mtok"]) == (280_000, 420_000)
    # 120 * 0.28 + 30 * 0.42 = 46.2 -> 47 at deepseek-chat's prices; gpt-4o's would be 120 * 2.5 + 30 * 10 = 600.
    assert harness.db.account(_PROJECT, _TASK)["spent_micro_usd"] == 47
    # The version recorded on the reservation is the one written with the bill.
    assert detail["pricing_version"] == row["pricing_version"] == f"litellm-{version('litellm')}"
