"""Recorded role processes are identified with psutil.Process.cmdline."""

from __future__ import annotations

import psutil
import pytest

from aether_worker.role_launch import _is_recorded_role


def test_recorded_role_reads_psutil_cmdline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    class _Process:
        def __init__(self, pid: int) -> None:
            seen.append(pid)

        def cmdline(self) -> list[str]:
            return ["python", "-m", "aether_worker.runner_process"]

    monkeypatch.setattr(psutil, "Process", _Process)
    assert _is_recorded_role(42) is True
    assert seen == [42]

    class _Denied:
        def __init__(self, _pid: int) -> None:
            raise psutil.AccessDenied(pid=7)

    monkeypatch.setattr(psutil, "Process", _Denied)
    assert _is_recorded_role(7) is False
