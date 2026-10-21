"""Waiting for a started AETHER process (worker, API) to report ready."""

from __future__ import annotations

import subprocess
import time


def wait_worker_ready(bucket: list[str], proc: subprocess.Popen[str], **budget: float) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        if any("aether-worker-ready" in line for line in bucket):
            return
        if proc.poll() is not None:
            raise AssertionError(f"进程提前退出 {proc.returncode}: {''.join(bucket[-40:])}")
        time.sleep(0.1)
    raise AssertionError(f"没有看到 aether-worker-ready: {''.join(bucket[-40:])}")
