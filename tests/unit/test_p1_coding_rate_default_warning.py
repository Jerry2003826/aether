"""The worker says once at startup which prices coding calls are billed at, or that none is usable.

Coding usage is billed only at configured input and output prices (ADR-F49), never 1 micro-USD per
token. With a usable price the worker logs coding_prices with its pricing_version, both prices and the
per-call hold derived from them. Without one it warns coding_prices_missing: coding jobs then fail with
pricing_missing, other jobs still run, so it is a warning, not a startup failure. A rejected value is
not echoed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from aether_worker import coding_jobs, service
from aether_worker.goal_effect import WorkerEndpoints

_NAME = "aether.worker.coding"
_IN = "AETHER_CODING_INPUT_MICRO_USD_PER_MTOK"
_OUT = "AETHER_CODING_OUTPUT_MICRO_USD_PER_MTOK"


@pytest.fixture(autouse=True)
def _plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (_IN, _OUT, "AETHER_LIVE_MODEL_ALIAS", "AETHER_CODING_CALL_RESERVE_MICRO_USD", "AETHER_CODING_CALL_RESERVE_TOKENS"):
        monkeypatch.delenv(name, raising=False)


def _price_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage().startswith(("coding_prices", "coding_rate_default"))]


def test_no_usable_price_logs_one_startup_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    lines = _price_lines(caplog)
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    assert lines[0].name == _NAME
    assert lines[0].getMessage().startswith("coding_prices_missing ")
    assert "pricing_missing" in lines[0].getMessage()
    assert "micro_usd_per_token" not in lines[0].getMessage()


def test_configured_prices_are_logged_with_the_hold_they_imply(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv(_IN, "280000")
    monkeypatch.setenv(_OUT, "420000")
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    assert [(record.levelno, record.getMessage()) for record in _price_lines(caplog)] == [
        (
            logging.INFO,
            (
                "coding_prices pricing_version=config-280000-420000 input_micro_usd_per_mtok=280000 "
                "output_micro_usd_per_mtok=420000 call_reserve_micro_usd=1721"
            ),
        )
    ]


def test_the_live_alias_is_logged_with_the_litellm_table_version(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from importlib.metadata import version

    monkeypatch.setenv("AETHER_LIVE_MODEL_ALIAS", "deepseek-chat")
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    messages = [record.getMessage() for record in _price_lines(caplog)]
    assert messages == [
        (
            f"coding_prices pricing_version=litellm-{version('litellm')} input_micro_usd_per_mtok=280000 "
            "output_micro_usd_per_mtok=420000 call_reserve_micro_usd=1721"
        )
    ]


@pytest.mark.parametrize("raw", ["abc", "-3", "1.5"])
def test_a_rejected_price_warns_without_echoing_it(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str) -> None:
    monkeypatch.setenv(_IN, raw)
    monkeypatch.setenv(_OUT, "420000")
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    lines = _price_lines(caplog)
    assert len(lines) == 1 and lines[0].getMessage().startswith("coding_prices_missing ")
    assert raw not in lines[0].getMessage()


def test_the_hold_follows_the_tokens_and_the_higher_price(monkeypatch: pytest.MonkeyPatch) -> None:
    prices = {"pricing_version": "config-280000-420000", "input_micro_usd_per_mtok": 280_000, "output_micro_usd_per_mtok": 420_000}
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_TOKENS", "8192")
    assert coding_jobs.coding_call_reserve_micro_usd(prices) == 3441
    # An explicit per-call hold still wins; an invalid one is ignored.
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", "7000")
    assert coding_jobs.coding_call_reserve_micro_usd(prices) == 7000
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", "0")
    assert coding_jobs.coding_call_reserve_micro_usd(prices) == 3441


class _Stop(Exception):
    pass


def test_serve_logs_the_prices_before_polling(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    seen_at_connect: list[int] = []
    monkeypatch.setattr(service, "configure", lambda endpoints: None)

    async def connect(*_args: object, **_kwargs: object) -> object:
        # Client.connect comes before the Temporal worker exists and before the first poll.
        seen_at_connect.append(len(_price_lines(caplog)))
        raise _Stop

    monkeypatch.setattr(service.Client, "connect", connect)
    monkeypatch.setattr(service, "stop_children", lambda children: None)
    monkeypatch.setattr(service, "stop_recorded_roles", lambda: None)
    endpoints = WorkerEndpoints(
        worker_url="",
        verifier_url="",
        dispatcher_url="",
        artifact_root=Path("/nonexistent"),
        temporal_address="127.0.0.1:1",
        task_queue="aether-test",
    )
    with pytest.raises(_Stop):
        asyncio.run(service.serve(endpoints))
    assert seen_at_connect == [1]
    assert len(_price_lines(caplog)) == 1
