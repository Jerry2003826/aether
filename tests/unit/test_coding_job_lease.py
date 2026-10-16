"""Cancel confirmation and the TTL lease. A live job is not reaped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aether_worker.coding_jobs import lease_blocks_reap, terminal_state_after_stop
from aether_worker.coding_sandbox import CANCEL_GRACE_SECONDS, container_arguments, container_name, stop_arguments


def test_stop_uses_a_ten_second_grace_and_checks_the_job_label(tmp_path) -> None:
    job_id = "6f1c1a2e-7b34-4c2a-9d10-111111111111"
    assert CANCEL_GRACE_SECONDS == 10
    assert container_name(job_id) == "aether-code-6f1c1a2e7b344c2a9d10111111111111"
    assert container_name(job_id) != container_name("6f1c1a2e-7b34-4c2a-9d10-222222222222")
    commands = stop_arguments(job_id)
    flat = [part for command in commands for part in command]
    assert "10" in flat
    assert f"label=aether.coding={job_id}" in flat
    args = container_arguments(tmp_path, "python:3.12-alpine", job_id=job_id)
    assert f"aether.coding={job_id}" in args
    assert "docker.sock" not in " ".join(args)


def test_live_lease_blocks_reap_and_unconfirmed_stop_is_not_terminal() -> None:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    assert lease_blocks_reap(deadline=now - timedelta(seconds=1), lease_until=now + timedelta(seconds=30), now=now) is True
    assert lease_blocks_reap(deadline=now + timedelta(minutes=5), lease_until=None, now=now) is True
    assert lease_blocks_reap(deadline=now - timedelta(seconds=1), lease_until=now - timedelta(seconds=1), now=now) is False
    assert lease_blocks_reap(deadline=now - timedelta(seconds=1), lease_until=None, now=now) is False
    assert terminal_state_after_stop(confirmed=False, cancel=True) is None
    assert terminal_state_after_stop(confirmed=False, cancel=False) is None
    assert terminal_state_after_stop(confirmed=True, cancel=True) == "CANCELLED"
    assert terminal_state_after_stop(confirmed=True, cancel=False) == "FAILED"
