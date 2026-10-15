"""Task workflow that materializes a start command. History stays free of direct I/O.

The start argument is either a task id or ``{"task_id", "intake_pause_seconds"}``.
The pause defaults to 0. A positive value is recorded in workflow history so a
restart keeps the same wait; production does not set it.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aether_worker.goal_effect import execute_task, prepare_task


@workflow.defn
class DurableTaskWorkflow:
    accepts_intake_pause = True

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
    async def run(self, task: str | dict) -> list[str]:
        task_id, pause = _start(task)
        while self._pending or not self._closed:
            await workflow.wait_condition(lambda: bool(self._pending) or self._closed)
            while self._pending:
                item = self._pending.pop(0)
                if item.get("project_id"):
                    activity_input = {
                        "task_id": task_id,
                        "project_id": str(item["project_id"]),
                        "command_id": str(item["command_id"]),
                    }
                    await workflow.execute_activity(prepare_task, activity_input, start_to_close_timeout=timedelta(seconds=60))
                    if pause > timedelta(0):
                        await workflow.sleep(pause)
                    await workflow.execute_activity(execute_task, activity_input, start_to_close_timeout=timedelta(seconds=60))
            if self._closed:
                break
        return list(self._applied)


def _start(task: str | dict) -> tuple[str, timedelta]:
    if isinstance(task, str):
        return task, timedelta(0)
    seconds = float(task.get("intake_pause_seconds") or 0)
    if seconds < 0:
        seconds = 0
    return str(task["task_id"]), timedelta(seconds=seconds)
