"""pytest plugin (-p tests.integration.r11_sandbox_hash_plugin) for test_r11_console_image_ready: the
OpenHands sandbox context hash becomes R11_FAKE_SANDBOX_HASH, a hash no image has been built for yet,
so the session starts as it would right after a change to a file the sandbox image copies."""

from __future__ import annotations

import os

from aether_worker import openhands_image


def pytest_configure(config) -> None:
    fake = os.environ.get("R11_FAKE_SANDBOX_HASH", "")
    if fake:
        openhands_image.context_hash = lambda root=None: fake
