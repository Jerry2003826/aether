"""Local OpenHands sandbox image. JOB_IMAGE stays the pinned alpine digest."""

from __future__ import annotations

from pathlib import Path

from aether_worker.container_runtime import build_context, image_id_with_label

OPENHANDS_VERSION = "1.54.0"
OPENHANDS_BASE = "python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f"
SANDBOX_LABEL = f"aether.openhands_sandbox={OPENHANDS_VERSION}"
SANDBOX_TAG = f"aether-openhands-sandbox:{OPENHANDS_VERSION}"
DOCKERFILE = "infra/openhands-sandbox.Dockerfile"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def coding_runtime_image() -> str | None:
    return image_id_with_label(SANDBOX_LABEL)


def launch_image(requested: str, pinned: str) -> str:
    """Product launches use the built image id. Any other requested image is returned unchanged."""
    if requested != pinned:
        return requested
    found = coding_runtime_image()
    if not found:
        raise LookupError("image_unavailable")
    return found


def build_openhands_sandbox() -> str:
    return build_context(
        root=repository_root(),
        dockerfile=DOCKERFILE,
        tag=SANDBOX_TAG,
        labels={"aether.openhands_sandbox": OPENHANDS_VERSION},
    )
