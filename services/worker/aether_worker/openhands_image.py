"""Local OpenHands sandbox image. JOB_IMAGE stays the pinned alpine digest."""

from __future__ import annotations

import hashlib
from pathlib import Path

from aether_worker.container_runtime import build_context, image_id_with_label

OPENHANDS_VERSION = "1.54.0"
OPENHANDS_BASE = "python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f"
SANDBOX_REPOSITORY = "aether-openhands-sandbox"
HASH_LABEL = "aether.openhands_sandbox_hash"
DOCKERFILE = "infra/openhands-sandbox.Dockerfile"
# Always hashed, even if the Dockerfile stops naming them: the environment and its lock files.
_CONTEXT_ROOTS = ("environments/openhands",)
_LOCK_FILES = ("environments/openhands/pyproject.toml", "environments/openhands/uv.lock")
_SKIP_PARTS = {".venv", "__pycache__", ".git"}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _copy_sources(dockerfile: str) -> list[str]:
    """Source paths of every COPY/ADD in the Dockerfile, relative to the build context."""
    found: list[str] = []
    for raw in dockerfile.splitlines():
        words = raw.split()
        if len(words) < 3 or words[0].upper() not in {"COPY", "ADD"}:
            continue
        args = [word for word in words[1:] if not word.startswith("--")]
        found.extend(item.rstrip("/") for item in args[:-1])
    return found


def _context_files(root: Path) -> list[Path]:
    dockerfile = root / DOCKERFILE
    names = {DOCKERFILE, *_LOCK_FILES, *_copy_sources(dockerfile.read_text(encoding="utf-8")), *_CONTEXT_ROOTS}
    files: set[Path] = set()
    for name in names:
        path = root / name
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in candidates:
            relative = item.relative_to(root)
            if any(part in _SKIP_PARTS for part in relative.parts) or not item.is_file():
                continue
            files.add(item)
        if not path.exists():
            raise FileNotFoundError(name)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def context_hash(root: Path | None = None) -> str:
    """SHA-256 over path and content of the Dockerfile, every file it copies, and the lock files."""
    base = repository_root() if root is None else root
    digest = hashlib.sha256()
    for path in _context_files(base):
        name = path.relative_to(base).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()[:16]


def sandbox_tag(root: Path | None = None) -> str:
    return f"{SANDBOX_REPOSITORY}:{OPENHANDS_VERSION}-{context_hash(root)}"


def sandbox_label(root: Path | None = None) -> str:
    return f"{HASH_LABEL}={context_hash(root)}"


def coding_runtime_image() -> str | None:
    return image_id_with_label(sandbox_label())


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
        tag=sandbox_tag(),
        labels={"aether.openhands_sandbox": OPENHANDS_VERSION, HASH_LABEL: context_hash()},
    )
