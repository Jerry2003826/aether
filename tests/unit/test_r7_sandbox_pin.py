"""S-064: sandbox and grader images are digest-pinned, and pytest is hash-locked."""

from __future__ import annotations

from aether_worker.f26_acceptance import VERIFY_IMAGE
from aether_worker.live_harness import ISOLATION_IMAGE
from aether_worker.runner import JOB_IMAGE
from aether_worker.sandbox_image import SANDBOX_IMAGE, grader_dockerfile, image_is_digest_pinned

MUTABLE = "python:3.12-alpine"


def test_sandbox_references_are_digest_pinned() -> None:
    for reference in (SANDBOX_IMAGE, JOB_IMAGE, ISOLATION_IMAGE, VERIFY_IMAGE):
        assert image_is_digest_pinned(reference)
        assert reference == SANDBOX_IMAGE
        assert MUTABLE not in reference


def test_grader_image_installs_pytest_with_hashes() -> None:
    dockerfile = grader_dockerfile()
    assert "pytest==8.3.5" in dockerfile
    assert "--require-hashes" in dockerfile
    assert SANDBOX_IMAGE in dockerfile
    assert MUTABLE not in dockerfile
    assert "pip install" in dockerfile
