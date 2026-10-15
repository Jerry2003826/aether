"""Activities own the side effects. Workflows only schedule them."""

from temporalio import activity

APPLIED: dict[str, int] = {}


@activity.defn
async def record_applied(payload: dict) -> int:
    task_id = str(payload["task_id"])
    APPLIED[task_id] = APPLIED.get(task_id, 0) + 1
    return APPLIED[task_id]
