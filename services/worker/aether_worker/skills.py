"""Skill packages. A manifest requests capabilities; it does not grant them."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

TRUSTED_PREFIXES = ("services/", "packages/", "migrations/", "contracts/", "apps/web/")
SECRET_MARKERS = ("sk-live-", "密钥", "api_key=")


class SkillError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _skill_file(root: Path) -> Path:
    path = root / "SKILL.md"
    if not path.is_file():
        raise SkillError("skill_missing")
    return path


def _frontmatter(root: Path) -> dict[str, Any]:
    header: list[str] = []
    with _skill_file(root).open(encoding="utf-8") as handle:
        if handle.readline().strip() != "---":
            raise SkillError("frontmatter_missing")
        for line in handle:
            if line.strip() == "---":
                break
            header.append(line)
        else:
            raise SkillError("frontmatter_missing")
    loaded = yaml.safe_load("".join(header)) or {}
    if not isinstance(loaded, dict) or not loaded.get("name") or not loaded.get("description"):
        raise SkillError("metadata_invalid")
    return {"name": str(loaded["name"]), "description": str(loaded["description"])}


def load_metadata(root: Path) -> dict[str, Any]:
    """Read only the SKILL.md frontmatter. Body and resources stay unread."""
    meta = _frontmatter(root)
    meta["loaded"] = ["metadata"]
    return meta


def load_body(root: Path) -> str:
    lines = _skill_file(root).read_text(encoding="utf-8").splitlines()
    end = lines[1:].index("---") + 1
    return "\n".join(lines[end + 1 :]).strip() + "\n"


def load_resource(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or path.suffix not in {".md", ".py", ".json"}:
        raise SkillError("resource_rejected")
    if not path.is_file():
        raise SkillError("resource_missing")
    return path.read_text(encoding="utf-8")


def script_sha256(root: Path, name: str) -> str:
    path = root / "scripts" / name
    if not path.is_file() or path.parent.resolve() != (root / "scripts").resolve():
        raise SkillError("script_rejected")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute_script(root: Path, name: str, *, approved_hashes: set[str], output: Path) -> dict[str, Any]:
    digest = script_sha256(root, name)
    if digest not in approved_hashes:
        return {"executed": False, "reason": "unapproved", "sha256": digest}
    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / name), str(output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return {"executed": completed.returncode == 0, "reason": "approved", "sha256": digest}


def redact_trace(text: str) -> str:
    redacted = text
    for marker in SECRET_MARKERS:
        if marker in redacted:
            redacted = redacted.replace(marker, "[REDACTED]")
    return redacted


def read_split(root: Path, split: str) -> str:
    if split not in {"train", "dev"}:
        raise SkillError("holdout_rejected")
    path = root / split / "trace.txt"
    return redact_trace(path.read_text(encoding="utf-8"))


def propose_candidate(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("auto_promote") is True or "policy.modify" in set(manifest.get("requested_capabilities") or []):
        raise SkillError("auto_privilege_rejected")
    for raw in manifest.get("edits") or []:
        relative = str(raw).replace("\\", "/")
        if relative.startswith(TRUSTED_PREFIXES) or "/../" in f"/{relative}/":
            raise SkillError("control_edit_rejected")
    return {"accepted": True, "status": "QUARANTINED", "auto_promote": False}
