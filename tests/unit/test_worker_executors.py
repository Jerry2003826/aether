"""Control delivery stays on its own pool when activity threads are busy."""

import asyncio
import threading

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
