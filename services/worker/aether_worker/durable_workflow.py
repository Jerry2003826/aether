"""Task workflow that materializes a start command. History stays free of direct I/O."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aether_worker.goal_effect import materialize_goal


@workflow.defn
class DurableTaskWorkflow:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._pending: list[dict] = []
        self._applied: list[str] = []
        self._closed = False

    @workflow.signal
    def command(self, payload: dict) -> None:
        command_id = str(payload["command_id"])
        if command_id in self._seen:
            return
        self._seen.add(command_id)
        self._applied.append(command_id)
        self._pending.append(dict(payload))

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def applied(self) -> list[str]:
        return list(self._applied)

    @workflow.run
    async def run(self, task_id: str) -> list[str]:
        while self._pending or not self._closed:
            await workflow.wait_condition(lambda: bool(self._pending) or self._closed)
            while self._pending:
                item = self._pending.pop(0)
                if item.get("project_id"):
                    await workflow.execute_activity(
                        materialize_goal,
                        {
                            "task_id": task_id,
                            "project_id": str(item["project_id"]),
                            "command_id": str(item["command_id"]),
                        },
                        start_to_close_timeout=timedelta(seconds=60),
                    )
            if self._closed:
                break
        return list(self._applied)
