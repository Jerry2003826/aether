"""Local OpenHands sandbox image. JOB_IMAGE stays the pinned alpine digest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from docker.errors import DockerException, ImageNotFound

from aether_worker.container_runtime import build_context, docker_client, image_id_with_label

OPENHANDS_VERSION = "1.54.0"
OPENHANDS_BASE = "python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f"
SANDBOX_REPOSITORY = "aether-openhands-sandbox"
HASH_LABEL = "aether.openhands_sandbox_hash"
DOCKERFILE = "infra/openhands-sandbox.Dockerfile"
# The runner answers a delegated "run" with this stderr when no sandbox image exists; the worker
# matches it to fail the job as image_unavailable. One constant so the two sides cannot drift.
IMAGE_UNAVAILABLE = "image_unavailable"
# Always hashed, even if the Dockerfile stops naming them: the environment and its lock files.
_CONTEXT_ROOTS = ("environments/openhands",)
_LOCK_FILES = ("environments/openhands/pyproject.toml", "environments/openhands/uv.lock")
_SKIP_PARTS = {".venv", "__pycache__", ".git"}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _instructions(dockerfile: str) -> list[str]:
    """Logical Dockerfile instructions: comments dropped, backslash continuations joined."""
    found: list[str] = []
    pending = ""
    for raw in dockerfile.splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if pending and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        found.append(pending + stripped)
        pending = ""
    if pending:
        found.append(pending)
    return found


def _copy_sources(dockerfile: str) -> list[str]:
    """Source paths of every build-context COPY/ADD, relative to the build context.

    Handles flags, backslash-continued lines and the JSON-array form. `--from=` copies come
    from another stage and are skipped. Heredocs, URLs, malformed JSON and a missing
    destination raise ValueError so the hash never silently misses an input.
    """
    found: list[str] = []
    for line in _instructions(dockerfile):
        keyword, _, rest = line.partition(" ")
        if keyword.upper() not in {"COPY", "ADD"}:
            continue
        words = rest.split()
        flags = [word for word in words if word.startswith("--")]
        if any(flag.startswith("--from") for flag in flags):
            continue
        body = rest.strip()
        while body.startswith("--"):
            body = body.split(None, 1)[1] if len(body.split(None, 1)) > 1 else ""
        if body.startswith("["):
            try:
                args = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ValueError(f"unparsed {keyword}: {line}") from exc
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise ValueError(f"unparsed {keyword}: {line}")
        else:
            args = body.split()
        if len(args) < 2:
            raise ValueError(f"{keyword} without source and destination: {line}")
        sources = args[:-1]
        for item in sources:
            if item.startswith("<<") or "://" in item:
                raise ValueError(f"unsupported {keyword} source: {item}")
        found.extend(item.rstrip("/") for item in sources)
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


class ImageUnavailable(LookupError):
    """No image for tag. reason is one of:

    image_unavailable        neither pull nor build was allowed
    image_pull_failed        the pull was tried and failed, and build was not allowed
    image_build_unsupported  build was allowed but this module can only build the OpenHands sandbox tag
    image_build_failed       the build was tried and failed
    image_label_mismatch     a freshly built sandbox image lacks the expected context-hash label
    """

    def __init__(self, reason: str, tag: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tag = tag


def _labels(image: Any) -> dict[str, str]:
    labels = getattr(image, "labels", None)
    return dict(labels) if isinstance(labels, dict) else {}


def ensure_image(client: Any, tag: str, *, allow_pull: bool = True, allow_build: bool = True) -> str:
    """Image id for tag: the local image if present, else a pull (allow_pull), else a build (allow_build).

    Only ImageNotFound on the local lookup counts as missing; any other Docker error
    (daemon unreachable) propagates. A pull that fails for any Docker reason (not found,
    access denied, registry down) falls through to the build. Only the current OpenHands
    sandbox tag can be built. For that tag every image, local, pulled or built, must carry
    HASH_LABEL equal to the current context hash; a local or pulled image without it counts
    as absent. When nothing yields an image this raises ImageUnavailable. Call it before
    anything waits on a readiness timeout: a first build takes minutes.
    """
    sandbox = tag == sandbox_tag()
    expected = context_hash() if sandbox else None

    def acceptable(image: Any) -> bool:
        return expected is None or _labels(image).get(HASH_LABEL) == expected

    try:
        local = client.images.get(tag)
    except ImageNotFound:
        local = None
    if local is not None and acceptable(local):
        return str(local.id)
    pull_error: BaseException | None = None
    if allow_pull:
        try:
            pulled = client.images.pull(tag)
        except DockerException as exc:
            pull_error = exc
        else:
            if acceptable(pulled):
                return str(pulled.id)
            pull_error = ImageUnavailable("image_label_mismatch", tag)
    if not allow_build:
        raise ImageUnavailable("image_pull_failed" if allow_pull else "image_unavailable", tag) from pull_error
    if not sandbox:
        raise ImageUnavailable("image_build_unsupported", tag) from pull_error
    try:
        built = build_openhands_sandbox()
        image = client.images.get(built)
    except (DockerException, OSError) as exc:
        raise ImageUnavailable("image_build_failed", tag) from exc
    if not acceptable(image):
        raise ImageUnavailable("image_label_mismatch", tag)
    return str(image.id)


def ensure_openhands_sandbox(client: Any | None = None) -> str:
    """The OpenHands sandbox image for the current context hash, local or built here.

    It is never pulled: the tag only exists on hosts that built it, and a registry image
    under that name is not this context.
    """
    return ensure_image(docker_client() if client is None else client, sandbox_tag(), allow_pull=False)
