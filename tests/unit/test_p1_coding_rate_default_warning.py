"""The worker says once at startup when the coding money rate is not taken from the environment.

Unset (or blank) or rejected AETHER_CODING_CALL_RESERVE_MICRO_USD makes the money per call equal to
the token count, so the rate is 1 micro-USD per token: a bookkeeping unit, not a price. That stays a
supported setup, so it is a warning, not a startup failure. A rejected value is not echoed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from aether_worker import coding_jobs, service
from aether_worker.goal_effect import WorkerEndpoints

_NAME = "aether.worker.coding"


def _message(reason: str, tokens: int) -> str:
    return (
        f"coding_rate_default AETHER_CODING_CALL_RESERVE_MICRO_USD={reason} default={tokens} micro_usd_per_call "
        f"(=AETHER_CODING_CALL_RESERVE_TOKENS={tokens}) rate=1 micro_usd_per_token accounting_unit_not_a_price"
    )


def _rate_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage().startswith("coding_rate_default")]


def test_an_unset_micro_rate_logs_one_startup_warning_naming_the_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", raising=False)
    monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_TOKENS", raising=False)
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    warnings = _rate_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].name == _NAME
    assert warnings[0].getMessage() == _message("unset", 4096)


def test_a_blank_micro_rate_is_unset(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", "   ")
    monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_TOKENS", raising=False)
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    assert [record.getMessage() for record in _rate_warnings(caplog)] == [_message("unset", 4096)]


def test_the_warning_names_the_token_count_actually_used(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", raising=False)
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_TOKENS", "8192")
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    assert [record.getMessage() for record in _rate_warnings(caplog)] == [_message("unset", 8192)]
    assert coding_jobs.coding_call_reserve_micro_usd() == 8192


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "1.5"])
def test_an_invalid_micro_rate_warns_as_invalid_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", raw)
    monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_TOKENS", raising=False)
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    warnings = _rate_warnings(caplog)
    assert [record.getMessage() for record in warnings] == [_message("invalid", 4096)]
    assert f"={raw} " not in warnings[0].getMessage()
    assert coding_jobs.coding_call_reserve_micro_usd() == 4096


def test_a_valid_micro_rate_logs_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", "7000")
    monkeypatch.setenv("AETHER_CODING_CALL_RESERVE_TOKENS", "3000")
    caplog.set_level(logging.DEBUG)
    coding_jobs.warn_coding_rate_default()
    assert _rate_warnings(caplog) == []


class _Stop(Exception):
    pass


def test_serve_warns_before_polling(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_MICRO_USD", raising=False)
    monkeypatch.delenv("AETHER_CODING_CALL_RESERVE_TOKENS", raising=False)
    caplog.set_level(logging.DEBUG)
    seen_at_connect: list[int] = []
    monkeypatch.setattr(service, "configure", lambda endpoints: None)

    async def connect(*_args: object, **_kwargs: object) -> object:
        # Client.connect comes before the Temporal worker exists and before the first poll.
        seen_at_connect.append(len(_rate_warnings(caplog)))
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
    assert len(_rate_warnings(caplog)) == 1
