"""Deterministic task workflow. Commands are deduplicated by command_id."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aether_worker.activities import record_applied


@workflow.defn
class TaskWorkflow:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._applied: list[str] = []
        self._closed = False

    @workflow.signal
    def command(self, payload: dict) -> None:
        command_id = str(payload["command_id"])
        if command_id in self._seen:
            return
        self._seen.add(command_id)
        self._applied.append(command_id)

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def applied(self) -> list[str]:
        return list(self._applied)

    @workflow.run
    async def run(self, task_id: str) -> list[str]:
        await workflow.wait_condition(lambda: self._closed)
        await workflow.execute_activity(
            record_applied,
            {"task_id": task_id, "count": len(self._applied)},
            start_to_close_timeout=timedelta(seconds=10),
        )
        return list(self._applied)
