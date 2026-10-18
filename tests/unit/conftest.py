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
