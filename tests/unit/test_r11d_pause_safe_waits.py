"""Integration wait helpers must not time out because the box was paused (r11d flake root cause).

On this KVM box a pause moves the wall clock far more than the monotonic clock (clockwatch
19:12:11: one 1 s sleep took wall 84.9 s, monotonic 15.3 s; the 2h40m pause on 2026-10-22). A wait
whose deadline is time.time() then expires with almost no active time spent. Both clocks are
scripted here; a real overrun must still fail.
"""

from __future__ import annotations

import time

import pytest

from tests.integration import test_sys01_entry, test_worker_process


class _Clock:
    def __init__(self, monkeypatch, *, jump_at: int | None, jump: float = 9600.0, mono_jump: float | None = None) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 1000.0
        self.sleeps = 0
        self.jump_at = jump_at
        self.jump = jump
        self.mono_jump = jump / 4 if mono_jump is None else mono_jump
        monkeypatch.setattr(time, "time", lambda: self.wall)
        monkeypatch.setattr(time, "monotonic", lambda: self.mono)
        monkeypatch.setattr(time, "sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.wall += seconds
        self.mono += seconds
        if self.jump_at is not None and self.sleeps == self.jump_at:
            self.wall += self.jump
            self.mono += self.mono_jump


class _Proc:
    returncode = None

    def poll(self):
        return None


class _Response:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


class _Http:
    def __init__(self, ready_after: int) -> None:
        self.calls = 0
        self.ready_after = ready_after

    def get(self, _url):
        self.calls += 1
        return _Response()


def test_poll_survives_a_paused_box(monkeypatch) -> None:
    _Clock(monkeypatch, jump_at=2)
    http = _Http(ready_after=5)
    found = test_sys01_entry._poll(http, "/x", lambda _body: "done" if http.calls >= 5 else None, _Proc(), [], 45)
    assert found == "done"


def test_poll_still_times_out_on_real_overrun(monkeypatch) -> None:
    _Clock(monkeypatch, jump_at=None)
    with pytest.raises(AssertionError):
        test_sys01_entry._poll(_Http(ready_after=10**9), "/x", lambda _body: None, _Proc(), [], 45)


def test_wait_line_survives_a_paused_box(monkeypatch) -> None:
    clock = _Clock(monkeypatch, jump_at=2)
    bucket: list[str] = []
    real_sleep = clock.sleep

    def sleep(seconds: float) -> None:
        real_sleep(seconds)
        if clock.sleeps == 5:
            bucket.append("aether-worker-ready\n")

    monkeypatch.setattr(time, "sleep", sleep)
    test_worker_process._wait_line(bucket, "aether-worker-ready", _Proc(), timeout=90)


def test_wait_line_still_times_out_on_real_overrun(monkeypatch) -> None:
    _Clock(monkeypatch, jump_at=None)
    with pytest.raises(AssertionError):
        test_worker_process._wait_line([], "aether-worker-ready", _Proc(), timeout=90)


def test_poll_counts_a_monotonic_gap_as_elapsed_time(monkeypatch) -> None:
    """A jump in time.monotonic() is time spent. It must expire the deadline, not be dropped as a pause."""
    _Clock(monkeypatch, jump_at=1, jump=0.0, mono_jump=10_000.0)
    http = _Http(ready_after=5)
    with pytest.raises(AssertionError):
        test_sys01_entry._poll(http, "/x", lambda _body: "done" if http.calls >= 5 else None, _Proc(), [], 45)


def test_wait_line_counts_a_monotonic_gap_as_elapsed_time(monkeypatch) -> None:
    clock = _Clock(monkeypatch, jump_at=1, jump=0.0, mono_jump=10_000.0)
    bucket: list[str] = []
    real_sleep = clock.sleep

    def sleep(seconds: float) -> None:
        real_sleep(seconds)
        if clock.sleeps == 5:
            bucket.append("aether-worker-ready\n")

    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(AssertionError):
        test_worker_process._wait_line(bucket, "aether-worker-ready", _Proc(), timeout=90)
