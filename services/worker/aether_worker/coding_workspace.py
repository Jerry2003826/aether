"""Candidate workspace carried in the coding job's base artifact."""

from __future__ import annotations

import difflib

from aether_worker.bundle_files import BundleError, load_object, parse_file_map

_KIND = "aether.coding_workspace"


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


def unified_patch(base: dict[str, str], files: dict[str, str]) -> str:
    chunks: list[str] = []
    for name in sorted(set(base) | set(files)):
        before = _lines(base.get(name, ""))
        after = _lines(files.get(name, ""))
        if before == after:
            continue
        chunks.extend(
            difflib.unified_diff(before, after, fromfile=f"a/{name}", tofile=f"b/{name}")
        )
    text = "".join(chunks)
    if not text.startswith("--- "):
        return "--- a/empty\n+++ b/empty\n"
    return text


def _lines(value: str) -> list[str]:
    if not value:
        return []
    lines = value.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines
