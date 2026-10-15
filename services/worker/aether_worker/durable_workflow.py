"""Task workflow that materializes a start command. History stays free of direct I/O.

The start argument is either a task id or ``{"task_id", "intake_pause_seconds"}``.
The pause defaults to 0. A positive value is recorded in workflow history so a
restart keeps the same wait; production does not set it.

``pause``, ``resume`` and ``cancel`` are separate commands. They are applied before
the next side effect. A positive intake pause can be interrupted by one of those
commands; older histories keep the original sleep.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aether_worker.goal_effect import apply_task_control, execute_task, prepare_task

_CONTROL = {"pause", "resume", "cancel"}


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

    def _has_control(self) -> bool:
        return any(str(item.get("kind") or "") in _CONTROL for item in self._pending)

    async def _drain_controls(self, task_id: str) -> bool:
        blocked = False
        while self._pending and str(self._pending[0].get("kind") or "") in _CONTROL:
            item = self._pending.pop(0)
            kind = str(item.get("kind") or "")
            controlled = await workflow.execute_activity(
                apply_task_control,
                _activity_input(task_id, item),
                start_to_close_timeout=timedelta(seconds=60),
            )
            if kind in {"pause", "cancel"} and controlled.get("status") in {"PAUSED", "CANCELLED"}:
                blocked = True
            elif kind == "resume" and controlled.get("resumed") is True:
                blocked = False
        return blocked

    @workflow.run
    async def run(self, task: str | dict) -> list[str]:
        task_id, pause = _start(task)
        while self._pending or not self._closed:
            await workflow.wait_condition(lambda: bool(self._pending) or self._closed)
            while self._pending:
                item = self._pending.pop(0)
                if not item.get("project_id"):
                    continue
                kind = str(item.get("kind") or "start")
                activity_input = _activity_input(task_id, item)
                if kind in _CONTROL:
                    controlled = await workflow.execute_activity(apply_task_control, activity_input, start_to_close_timeout=timedelta(seconds=60))
                    if kind == "resume" and controlled.get("resumed") is True:
                        await workflow.execute_activity(execute_task, activity_input, start_to_close_timeout=timedelta(seconds=60))
                    continue
                prepared = await workflow.execute_activity(prepare_task, activity_input, start_to_close_timeout=timedelta(seconds=60))
                if prepared.get("status") == "prepared" and pause > timedelta(0):
                    if workflow.patched("intake-pause-interruptible"):
                        try:
                            await workflow.wait_condition(lambda: self._has_control(), timeout=pause)
                        except TimeoutError:
                            pass
                    else:
                        await workflow.sleep(pause)
                blocked = False
                if prepared.get("status") == "prepared":
                    blocked = await self._drain_controls(task_id)
                if prepared.get("status") == "prepared" and not blocked:
                    await workflow.execute_activity(execute_task, activity_input, start_to_close_timeout=timedelta(seconds=60))
            if self._closed:
                break
        return list(self._applied)


def _activity_input(task_id: str, item: dict) -> dict[str, str]:
    return {
        "task_id": task_id,
        "project_id": str(item["project_id"]),
        "command_id": str(item["command_id"]),
    }


def _start(task: str | dict) -> tuple[str, timedelta]:
    if isinstance(task, str):
        return task, timedelta(0)
    seconds = float(task.get("intake_pause_seconds") or 0)
    if seconds < 0:
        seconds = 0
    return str(task["task_id"]), timedelta(seconds=seconds)
