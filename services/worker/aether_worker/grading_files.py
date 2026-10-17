"""Which paths in a coding workspace belong to grading. Stdlib only.

The sandbox entry restores every grading path to its baseline before the protocol
runs, and patch publishing can drop grading paths from the agent's diff.

Interface:
    CANDIDATE_FILES        relative paths the agent is asked to change.
    is_grading_path(rel)   True for every other relative path, including cache/build dirs.
    is_cache_path(rel)     True for paths inside cache/build dirs (never part of a baseline).
    grading_paths(root)    relative POSIX paths of the grading files that exist under root.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

CANDIDATE_FILES = frozenset({"app.py"})
CACHE_DIRS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".tox",
        ".nox",
        ".eggs",
        "build",
        "dist",
        ".aether-state",
    }
)
IGNORED_DIRS = frozenset({".git"})


def _parts(relative: str | PurePosixPath) -> tuple[str, ...]:
    return tuple(part for part in PurePosixPath(str(relative).replace("\\", "/")).parts if part not in {"", "."})


def is_cache_path(relative: str | PurePosixPath) -> bool:
    return any(part in CACHE_DIRS or part.endswith(".egg-info") for part in _parts(relative))


def is_grading_path(relative: str | PurePosixPath) -> bool:
    """True unless `relative` is a candidate file. Cache and build directories are always grading."""
    parts = _parts(relative)
    if is_cache_path(relative):
        return True
    return "/".join(parts) not in CANDIDATE_FILES


def grading_paths(root: Path) -> set[str]:
    """Relative POSIX paths of grading files (and symlinks) under root. `.git` is skipped."""
    found: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        name = relative.as_posix()
        if is_grading_path(name):
            found.add(name)
    return found
