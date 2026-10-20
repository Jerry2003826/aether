"""Shared unit-test setup."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _worker_state_outside_the_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    # A coding container's labels carry the worker instance id, which is kept in the worker state
    # directory (default data/worker under the current directory); tests never write it into the checkout.
    monkeypatch.setenv("AETHER_WORKER_STATE_DIR", str(tmp_path_factory.getbasetemp() / "worker-state"))
    yield


@pytest.fixture(autouse=True)
def _configured_coding_prices(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Coding calls are billed only at configured prices (ADR-F49); without one a coding job fails with
    # pricing_missing. Tests that do not test pricing run at 1 micro-USD per token for input and
    # output alike, set explicitly here; test_p1_coding_pricing clears these and sets its own.
    monkeypatch.setenv("AETHER_CODING_INPUT_MICRO_USD_PER_MTOK", "1000000")
    monkeypatch.setenv("AETHER_CODING_OUTPUT_MICRO_USD_PER_MTOK", "1000000")
    yield
