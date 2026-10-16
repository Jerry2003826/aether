"""Task workflow that materializes a start command. History stays free of direct I/O.

The start argument is either a task id or ``{"task_id", "intake_pause_seconds"}``.
The pause defaults to 0. A positive value is recorded in workflow history so a
restart keeps the same wait; production does not set it.

``pause``, ``resume`` and ``cancel`` are separate commands. They are applied before
the next side effect. A positive intake pause can be interrupted by one of those
commands; older histories keep the original sleep. Histories recorded before
``execute-interruptible`` still wait for the whole execute activity. Newer runs
apply a pause or cancel while that activity is in flight, and the activity keeps
the provider receipt.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.common import RetryPolicy

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
        for item in extract_pending_controls(self._pending):
            kind = str(item.get("kind") or "")
            controlled = await workflow.execute_activity(
                apply_task_control,
                _activity_input(task_id, item),
                **_activity_kwargs(),
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
                    controlled = await workflow.execute_activity(apply_task_control, activity_input, **_activity_kwargs())
                    if kind == "resume" and controlled.get("resumed") is True:
                        await self._execute_task(activity_input)
                    continue
                prepared = await workflow.execute_activity(prepare_task, activity_input, **_activity_kwargs())
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
                    await self._execute_task(activity_input)
            if self._closed:
                break
        return list(self._applied)

    async def _execute_task(self, activity_input: dict[str, str]) -> None:
        """Let a queued pause or cancel reach the database while execute is running."""
        if workflow.patched("execute-interruptible"):
            handle = workflow.start_activity(
                execute_task,
                activity_input,
                cancellation_type=workflow.ActivityCancellationType.TRY_CANCEL,
                **_activity_kwargs(),
            )
            while not handle.done():
                await workflow.wait_condition(lambda: handle.done() or self._has_control())
                if not self._has_control():
                    break
                await self._drain_controls(activity_input["task_id"])
            await handle
            return
        await workflow.execute_activity(execute_task, activity_input, **_activity_kwargs())


def extract_pending_controls(pending: list[dict]) -> list[dict]:
    """Pull pause, resume and cancel out even when a non-control command is in front."""
    controls: list[dict] = []
    kept: list[dict] = []
    for item in pending:
        if str(item.get("kind") or "") in _CONTROL:
            controls.append(item)
        else:
            kept.append(item)
    pending[:] = kept
    return controls


def _activity_kwargs() -> dict[str, object]:
    """Bound retries on new runs. Histories recorded before this patch keep the old options."""
    if workflow.patched("bounded-activity-retry"):
        return {
            "start_to_close_timeout": timedelta(seconds=60),
            "heartbeat_timeout": timedelta(seconds=20),
            "retry_policy": RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["ArtifactError"],
            ),
        }
    return {"start_to_close_timeout": timedelta(seconds=60)}


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
