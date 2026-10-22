"""trigger_watch must be given the artifact store, so a watch run cannot skip its artifact."""

from __future__ import annotations

import pytest
from aether_worker.watches import trigger_watch


class _UntouchedDatabase:
    def transaction(self, **_kwargs):
        raise AssertionError("trigger_watch opened a transaction without an artifact store")


def test_trigger_watch_without_a_store_is_rejected_before_any_database_work() -> None:
    with pytest.raises(TypeError, match="store"):
        trigger_watch(
            _UntouchedDatabase(),
            owner_id="owner",
            project_id="project",
            watch_id="watch",
            watermark="v1",
            trigger_key="tick-1",
        )
