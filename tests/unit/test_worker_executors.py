"""Control delivery stays on its own pool when activity threads are busy."""

import asyncio
import threading
from pathlib import Path

from aether_worker.goal_effect import WorkerEndpoints, apply_task_control, configure, run_control_activity, set_control_executor
from aether_worker.service import make_executors, run_control


def test_control_work_runs_while_the_activity_pool_is_full() -> None:
    activity, control = make_executors()

    async def scenario() -> str:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(activity)
        release = threading.Event()

        def block() -> None:
            release.wait(2)

        filled = [loop.run_in_executor(None, block) for _ in range(8)]
        await asyncio.sleep(0.05)
        try:
            return await asyncio.wait_for(run_control(control, lambda: "ok"), timeout=0.5)
        finally:
            release.set()
            await asyncio.gather(*filled)

    try:
        assert asyncio.run(scenario()) == "ok"
    finally:
        activity.shutdown(wait=False, cancel_futures=True)
        control.shutdown(wait=False, cancel_futures=True)


def test_apply_task_control_uses_the_control_pool_when_activity_threads_are_full(monkeypatch) -> None:
    activity, control = make_executors()
    seen: list[str] = []

    def effect(_endpoints, _payload):
        seen.append(threading.current_thread().name)
        return {"status": "PAUSED", "resumed": False}

    monkeypatch.setattr("aether_worker.goal_effect.apply_task_control_effect", effect)
    configure(
        WorkerEndpoints(
            worker_url="postgresql://unused",
            verifier_url="postgresql://unused",
            dispatcher_url="postgresql://unused",
            artifact_root=Path("."),
            temporal_address="127.0.0.1:9",
            task_queue="control-pool",
        )
    )
    set_control_executor(control)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(activity)
        release = threading.Event()
        filled = [loop.run_in_executor(None, lambda: release.wait(2)) for _ in range(8)]
        await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(apply_task_control({"project_id": "p", "task_id": "t", "command_id": "c"}), timeout=0.5)
        finally:
            release.set()
            await asyncio.gather(*filled)

    try:
        asyncio.run(scenario())
    finally:
        set_control_executor(None)
        activity.shutdown(wait=False, cancel_futures=True)
        control.shutdown(wait=False, cancel_futures=True)
    assert seen and seen[0].startswith("aether-control")
