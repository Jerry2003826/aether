"""Research workflow. Side effects stay in the activity."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aether_worker.research import record_research_effect


@workflow.defn
class ResearchWorkflow:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._pending: list[dict] = []
        self._done: list[str] = []
        self._closed = False

    @workflow.signal
    def command(self, payload: dict) -> None:
        command_id = str(payload["command_id"])
        if command_id in self._seen:
            return
        self._seen.add(command_id)
        self._pending.append({"command_id": command_id})

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, task_id: str) -> list[str]:
        while self._pending or not self._closed:
            await workflow.wait_condition(lambda: bool(self._pending) or self._closed)
            while self._pending:
                item = self._pending.pop(0)
                await workflow.execute_activity(
                    record_research_effect,
                    {"task_id": task_id, "command_id": item["command_id"]},
                    start_to_close_timeout=timedelta(seconds=10),
                )
                self._done.append(item["command_id"])
            if self._closed:
                break
        return list(self._done)
