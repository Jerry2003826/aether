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

Runs started before ``task-command-versions`` were recorded by releases whose
control handling carried no marker (ADR-F27, P1-07). Until a marker proves a later
release, each point where the first release behaved differently asks a numbered
patch: replaying an old history keeps the first release's behavior there, and a
command that arrives after the upgrade gets the current handling.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.common import RetryPolicy

    from aether_worker.goal_effect import apply_task_control, execute_task, prepare_task

_CONTROL = {"pause", "resume", "cancel"}
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
_VERSIONED = "task-command-versions"
_BOUNDED = "task-run-bounded"
_LEGACY_POINT = "task-command-legacy-"
_DEFAULT_HISTORY_LIMIT = 5000


@workflow.defn
class DurableTaskWorkflow:
    accepts_intake_pause = True

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._pending: list[dict] = []
        self._applied: list[str] = []
        self._closed = False
        self._current = False
        self._bounded = False
        self._history_limit = _DEFAULT_HISTORY_LIMIT
        self._legacy_points = 0

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

    def _handles_controls(self) -> bool:
        """False only where an unmarked history shows the first release treating every command as a start."""
        if self._current:
            return True
        point = f"{_LEGACY_POINT}{self._legacy_points}"
        self._legacy_points += 1
        if workflow.patched(point):
            self._current = True
        return self._current

    def _activity_kwargs(self) -> dict[str, object]:
        """Bound retries on new runs. Histories recorded before this patch keep the old options."""
        if workflow.patched("bounded-activity-retry"):
            self._current = True
            return {
                "start_to_close_timeout": timedelta(seconds=60),
                "heartbeat_timeout": timedelta(seconds=20),
                "retry_policy": RetryPolicy(
                    maximum_attempts=3,
                    non_retryable_error_types=["ArtifactError"],
                ),
            }
        return {"start_to_close_timeout": timedelta(seconds=60)}

    async def _drain_controls(self, task_id: str) -> bool:
        blocked = False
        for item in extract_pending_controls(self._pending):
            kind = str(item.get("kind") or "")
            controlled = await workflow.execute_activity(
                apply_task_control,
                _activity_input(task_id, item),
                **self._activity_kwargs(),
            )
            self._note_terminal(controlled.get("status"))
            if kind in {"pause", "cancel"} and controlled.get("status") in {"PAUSED", "CANCELLED"}:
                blocked = True
            elif kind == "resume" and controlled.get("resumed") is True:
                blocked = False
        return blocked

    def _note_terminal(self, status: object) -> None:
        if self._bounded and str(status or "") in _TERMINAL:
            self._closed = True

    def _should_continue(self) -> bool:
        if self._closed:
            return False
        info = workflow.info()
        if info.is_continue_as_new_suggested():
            return True
        return info.get_current_history_length() >= self._history_limit

    def _cursor(self, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "intake_pause_seconds": 0,
            "intake_done": True,
            "history_event_limit": self._history_limit,
            "seen": list(self._seen),
            "applied": list(self._applied),
            "pending": list(self._pending),
        }

    @workflow.run
    async def run(self, task: str | dict) -> list[str]:
        task_id, pause, limit = _start(task)
        self._history_limit = limit
        self._current = workflow.patched(_VERSIONED)
        self._bounded = workflow.patched(_BOUNDED)
        if self._bounded and isinstance(task, dict):
            if task.get("intake_done"):
                pause = timedelta(0)
            if "seen" in task:
                self._seen = {str(item) for item in task.get("seen") or []}
                self._applied = [str(item) for item in task.get("applied") or []]
                self._pending = [dict(item) for item in task.get("pending") or []]
        while self._pending or not self._closed:
            await workflow.wait_condition(lambda: bool(self._pending) or self._closed)
            while self._pending:
                item = self._pending.pop(0)
                if not item.get("project_id"):
                    continue
                kind = str(item.get("kind") or "start")
                activity_input = _activity_input(task_id, item)
                legacy: bool | None = None
                if kind in _CONTROL:
                    legacy = not self._handles_controls()
                    if not legacy:
                        controlled = await workflow.execute_activity(apply_task_control, activity_input, **self._activity_kwargs())
                        self._note_terminal(controlled.get("status"))
                        if kind == "resume" and controlled.get("resumed") is True:
                            executed = await self._execute_task(activity_input)
                            self._note_terminal(executed.get("status"))
                        if self._bounded and self._should_continue():
                            workflow.continue_as_new(self._cursor(task_id))
                        continue
                prepared = await workflow.execute_activity(prepare_task, activity_input, **self._activity_kwargs())
                self._note_terminal(prepared.get("status"))
                if prepared.get("status") != "prepared":
                    if legacy is None:
                        legacy = not self._handles_controls()
                    if legacy:
                        if pause > timedelta(0):
                            await workflow.sleep(pause)
                        await self._execute_task(activity_input)
                    if self._bounded and self._should_continue():
                        workflow.continue_as_new(self._cursor(task_id))
                    continue
                if pause > timedelta(0):
                    if workflow.patched("intake-pause-interruptible"):
                        self._current = True
                        try:
                            await workflow.wait_condition(lambda: self._has_control(), timeout=pause)
                        except TimeoutError:
                            pass
                    else:
                        await workflow.sleep(pause)
                blocked = False
                if self._has_control():
                    if legacy is None:
                        legacy = not self._handles_controls()
                    if not legacy:
                        blocked = await self._drain_controls(task_id)
                if not blocked:
                    executed = await self._execute_task(activity_input)
                    self._note_terminal(executed.get("status"))
                if self._bounded and self._should_continue():
                    workflow.continue_as_new(self._cursor(task_id))
            if self._closed:
                break
        return list(self._applied)

    async def _execute_task(self, activity_input: dict[str, str]) -> dict:
        """Let a queued pause or cancel reach the database while execute is running."""
        if workflow.patched("execute-interruptible"):
            self._current = True
            handle = workflow.start_activity(
                execute_task,
                activity_input,
                cancellation_type=workflow.ActivityCancellationType.TRY_CANCEL,
                **self._activity_kwargs(),
            )
            while not handle.done():
                await workflow.wait_condition(lambda: handle.done() or self._has_control())
                if not self._has_control():
                    break
                await self._drain_controls(activity_input["task_id"])
            result = await handle
            return result if isinstance(result, dict) else {}
        result = await workflow.execute_activity(execute_task, activity_input, **self._activity_kwargs())
        return result if isinstance(result, dict) else {}


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


def _activity_input(task_id: str, item: dict) -> dict[str, str]:
    return {
        "task_id": task_id,
        "project_id": str(item["project_id"]),
        "command_id": str(item["command_id"]),
    }


def _start(task: str | dict) -> tuple[str, timedelta, int]:
    if isinstance(task, str):
        return task, timedelta(0), _DEFAULT_HISTORY_LIMIT
    seconds = float(task.get("intake_pause_seconds") or 0)
    if seconds < 0:
        seconds = 0
    raw_limit = task.get("history_event_limit") or _DEFAULT_HISTORY_LIMIT
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_HISTORY_LIMIT
    if limit < 1:
        limit = _DEFAULT_HISTORY_LIMIT
    return str(task["task_id"]), timedelta(seconds=seconds), limit
