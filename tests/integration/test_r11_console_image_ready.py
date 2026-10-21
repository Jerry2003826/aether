"""The integration session builds the OpenHands sandbox image for the current context before any
test runs, and a coding job without that image fails the console test as image_unavailable, not as a
model that was never called (r11: test_console_coding_job_matches_the_database, authorizations empty)."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys

import pytest
from aether_worker import openhands_image

from tests.integration import test_console_records


def test_a_fresh_session_builds_the_sandbox_image_for_a_context_no_image_exists_for() -> None:
    fake = secrets.token_hex(8)
    env = {**os.environ, "R11_FAKE_SANDBOX_HASH": fake}
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "tests.integration.r11_sandbox_hash_plugin",
                "tests/integration/r11_image_probe.py",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
    finally:
        subprocess.run(
            ["docker", "rmi", f"{openhands_image.SANDBOX_REPOSITORY}:{openhands_image.OPENHANDS_VERSION}-{fake}"],
            capture_output=True,
            check=False,
        )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]


def test_a_coding_job_without_its_image_fails_the_console_test_as_image_unavailable(client, role_urls, monkeypatch) -> None:
    fake = secrets.token_hex(8)
    monkeypatch.setattr(openhands_image, "context_hash", lambda root=None: fake)
    with pytest.raises(AssertionError) as failure:
        test_console_records.test_console_coding_job_matches_the_database(client, role_urls, monkeypatch)
    assert "image_unavailable" in str(failure.value), str(failure.value)
