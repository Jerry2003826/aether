"""Candidate workspace carried in the coding job's base artifact."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from aether_worker.bundle_files import BundleError, load_object, parse_file_map

_KIND = "aether.coding_workspace"
# Published when the candidate files equal the base files; not a patch git can apply.
UNCHANGED_PATCH = "--- a/empty\n+++ b/empty\n"


class WorkspaceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_workspace(data: bytes) -> dict[str, dict[str, str]]:
    try:
        body = load_object(data)
    except BundleError as exc:
        raise WorkspaceError("workspace_invalid") from exc
    if body.get("kind") != _KIND:
        raise WorkspaceError("workspace_invalid")
    try:
        base = parse_file_map(body.get("base_files") or {}, allow_empty=True)
        files = parse_file_map(body.get("files"))
        tests = parse_file_map(body.get("tests"))
    except BundleError as exc:
        raise WorkspaceError(exc.code if exc.code == "path_rejected" else "workspace_invalid") from exc
    if set(files) & set(tests):
        raise WorkspaceError("workspace_invalid")
    return {"base_files": base, "files": files, "tests": tests}


def unified_patch(base: Mapping[str, str | bytes], files: Mapping[str, str | bytes]) -> str:
    """The candidate change, written by git itself (`git diff --no-index --binary`).

    Each file is written out byte for byte (str as UTF-8, bytes as is) into a before and an after
    tree, so new, deleted and empty files, a missing final newline and a change only to it all take
    git's own form, and `git apply` reproduces the after files exactly. A file whose bytes are not
    UTF-8 on either side goes out as a GIT binary patch, so the patch text stays valid UTF-8.
    Nothing changed gives the unchanged marker.
    """
    old = {name: _raw(value) for name, value in base.items()}
    new = {name: _raw(value) for name, value in files.items()}
    changed = [name for name in sorted(set(old) | set(new)) if old.get(name) != new.get(name)]
    if not changed:
        return UNCHANGED_PATCH
    binary = {name for name in changed if not (_utf8(old.get(name)) and _utf8(new.get(name)))}
    text = [name for name in changed if name not in binary]
    parts = []
    if text:
        parts.append(_git_diff(old, new, text, force_binary=False))
    if binary:
        parts.append(_git_diff(old, new, sorted(binary), force_binary=True))
    return "".join(parts)


def _raw(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _utf8(value: bytes | None) -> bool:
    if value is None:
        return True
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _git_diff(old: dict[str, bytes], new: dict[str, bytes], names: list[str], *, force_binary: bool) -> str:
    """Run git diff --no-index --binary on two trees holding `names`; a/ and b/ are the tree names."""
    with tempfile.TemporaryDirectory(prefix="aether-patch-") as scratch:
        root = Path(scratch)
        try:
            for side, tree in (("a", old), ("b", new)):
                (root / side).mkdir()
                for name in names:
                    if name in tree:
                        path = root / side / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(tree[name])
                        path.chmod(0o644)
            attributes = root / "attributes"
            attributes.write_text("* binary\n" if force_binary else "", encoding="utf-8")
        except OSError as exc:
            raise WorkspaceError("workspace_invalid") from exc
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": scratch,
            "XDG_CONFIG_HOME": scratch,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CEILING_DIRECTORIES": str(root.parent),
            "LC_ALL": "C",
        }
        try:
            completed = subprocess.run(
                [
                    "git", "-c", f"core.attributesFile={attributes}", "diff", "--no-index", "--binary",
                    "--no-prefix", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", "a", "b",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError("patch_unavailable") from exc
    # --no-index exits 1 when the trees differ and 0 when they do not; anything else is git failing.
    if completed.returncode not in {0, 1}:
        raise WorkspaceError("patch_unavailable")
    return completed.stdout.decode("utf-8")
