"""Watch workflow. It performs no I/O; the activity side owns dispatch.

New runs record ``watch-run-activity``. Histories from before that marker, and
schedules that only pass a watch id, still return the id without an activity.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.common import RetryPolicy

    from aether_worker.watch_tick import run_watch_tick


@workflow.defn
class WatchWorkflow:
    @workflow.run
    async def run(self, watch_id: str, project_id: str = "", owner_id: str = "") -> str:
        if workflow.patched("watch-run-activity") and project_id and owner_id:
            await workflow.execute_activity(
                run_watch_tick,
                {"watch_id": watch_id, "project_id": project_id, "owner_id": owner_id},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        return watch_id
