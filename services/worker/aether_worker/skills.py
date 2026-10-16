"""Skill packages. A manifest requests capabilities; it does not grant them."""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any

import frontmatter

from aether_worker.runner import JOB_IMAGE, _docker, _ensure_image, docker_available

TRUSTED_PREFIXES = ("services/", "packages/", "migrations/", "contracts/", "apps/web/")
_SECRET_PATTERNS = (
    re.compile(r"sk-live-[A-Za-z0-9_-]+"),
    re.compile(r"api_key=\S+"),
    re.compile(r"密钥\S*"),
)


class SkillError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _skill_file(root: Path) -> Path:
    path = root / "SKILL.md"
    if not path.is_file():
        raise SkillError("skill_missing")
    return path


def _parsed(root: Path) -> frontmatter.Post:
    raw = _skill_file(root).read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise SkillError("frontmatter_missing")
    post = frontmatter.loads(raw)
    if not post.metadata:
        raise SkillError("frontmatter_missing")
    return post


def _frontmatter(root: Path) -> dict[str, Any]:
    loaded = _parsed(root).metadata
    if not isinstance(loaded, dict) or not loaded.get("name") or not loaded.get("description"):
        raise SkillError("metadata_invalid")
    return {"name": str(loaded["name"]), "description": str(loaded["description"])}


def load_metadata(root: Path) -> dict[str, Any]:
    """Read only the SKILL.md frontmatter. Body and resources stay unread."""
    meta = _frontmatter(root)
    meta["loaded"] = ["metadata"]
    return meta


def load_body(root: Path) -> str:
    return _parsed(root).content.strip() + "\n"


def load_resource(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or path.suffix not in {".md", ".py", ".json"}:
        raise SkillError("resource_rejected")
    if not path.is_file():
        raise SkillError("resource_missing")
    return path.read_text(encoding="utf-8")


def _script_bytes(root: Path) -> dict[str, bytes]:
    scripts = root / "scripts"
    if not scripts.is_dir():
        raise SkillError("script_rejected")
    files: dict[str, bytes] = {}
    for path in sorted(scripts.rglob("*")):
        if path.is_symlink():
            raise SkillError("script_rejected")
        if not path.is_file():
            continue
        relative = path.relative_to(scripts).as_posix()
        if ".." in relative.split("/"):
            raise SkillError("script_rejected")
        files[relative] = path.read_bytes()
    if not files:
        raise SkillError("script_rejected")
    return files


def scripts_tree_sha256(files: dict[str, bytes]) -> str:
    lines = [f"{hashlib.sha256(data).hexdigest()} {name}" for name, data in sorted(files.items())]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def execute_script(root: Path, name: str, *, approved_hashes: set[str], output: Path) -> dict[str, Any]:
    """Hash the scripts directory as it is now, then run only that byte snapshot."""
    files = _script_bytes(root)
    digest = scripts_tree_sha256(files)
    if name not in files or digest not in approved_hashes:
        return {"executed": False, "reason": "unapproved", "sha256": digest}
    if not docker_available():
        raise SkillError("sandbox_unavailable")
    _ensure_image()
    with tempfile.TemporaryDirectory(prefix="aether-skill-") as temporary:
        script_root = Path(temporary) / "scripts"
        out_root = Path(temporary) / "out"
        script_root.mkdir()
        out_root.mkdir()
        for relative, data in files.items():
            destination = script_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        completed = _docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--tmpfs",
                "/run/aether:rw,nosuid,nodev,mode=0700",
                "-e",
                "HOME=/run/aether",
                "-e",
                "TMPDIR=/run/aether",
                "-e",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "-e",
                "LANG=C.UTF-8",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-v",
                f"{script_root}:/work/scripts:ro",
                "-v",
                f"{out_root}:/work/out:rw",
                JOB_IMAGE,
                "python",
                "-I",
                "-c",
                "import runpy, sys; sys.path.append('/work/scripts'); sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')",
                f"/work/scripts/{name}",
                "/work/out/marker.txt",
            ]
        )
        marker = out_root / "marker.txt"
        if marker.is_file():
            output.write_bytes(marker.read_bytes())
    return {"executed": completed.returncode == 0 and output.is_file(), "reason": "approved", "sha256": digest}


def redact_trace(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def read_split(root: Path, split: str) -> str:
    if split not in {"train", "dev"}:
        raise SkillError("holdout_rejected")
    path = root / split / "trace.txt"
    return redact_trace(path.read_text(encoding="utf-8"))


def normalize_edit_path(raw: str) -> str:
    text = str(raw).replace("\\", "/").strip()
    if text.lower().startswith("file://"):
        text = text[7:]
    if len(text) >= 2 and text[1] == ":":
        text = text[2:]
    parts: list[str] = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise SkillError("control_edit_rejected")
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _trusted_edit(relative: str) -> bool:
    lowered = relative.lower()
    prefixes = tuple(prefix.lower() for prefix in TRUSTED_PREFIXES)
    segments = lowered.split("/")
    for index in range(len(segments)):
        suffix = "/".join(segments[index:])
        for prefix in prefixes:
            bare = prefix.rstrip("/")
            if suffix == bare or suffix.startswith(prefix):
                return True
    return False


def propose_candidate(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("auto_promote") is True or "policy.modify" in set(manifest.get("requested_capabilities") or []):
        raise SkillError("auto_privilege_rejected")
    for raw in manifest.get("edits") or []:
        if _trusted_edit(normalize_edit_path(str(raw))):
            raise SkillError("control_edit_rejected")
    return {"accepted": True, "status": "QUARANTINED", "auto_promote": False}
