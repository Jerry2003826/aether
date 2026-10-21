"""Run only by test_r11_console_image_ready in its own pytest session (not a test_* module)."""

from __future__ import annotations

from aether_worker.openhands_image import coding_runtime_image, sandbox_tag


def test_the_sandbox_image_for_this_context_exists_before_the_first_test() -> None:
    assert coding_runtime_image() is not None, f"no image labelled for {sandbox_tag()}"
