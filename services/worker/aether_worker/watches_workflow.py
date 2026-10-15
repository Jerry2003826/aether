"""Watch workflow. It performs no I/O; the activity side owns dispatch."""

from __future__ import annotations

from temporalio import workflow


@workflow.defn
class WatchWorkflow:
    @workflow.run
    async def run(self, watch_id: str) -> str:
        return watch_id
