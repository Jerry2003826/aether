"""Which paths in a coding workspace belong to grading. Stdlib only; the single source of truth.

Grading files are an explicit denylist. Everything else is agent-editable source: the agent's
edits and new files there are kept for grading and published in the patch.

Interface (`rel` is a path relative to the workspace root, POSIX or native separators):
    is_grading_path(rel, editable=None)  True for test files, grading support/config files,
                                         interpreter startup hooks and cache/build dirs.
                                         `editable` (a job's explicit editable-file list, when
                                         the job has one) wins over the denylist, except for
                                         startup hooks and cache paths.
    is_startup_hook(rel)                 sitecustomize.py, usercustomize.py, *.pth. An agent that
                                         adds one fails the job.
    is_cache_path(rel)                   paths inside cache/build dirs; never part of a baseline.
    grading_paths(root, editable=None)   relative POSIX paths of grading files under root (.git skipped).
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path, PurePosixPath

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
TEST_DIRS = frozenset({"tests", "test"})
SUPPORT_FILES = frozenset({"oracle.py", "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"})
STARTUP_HOOKS = frozenset({"sitecustomize.py", "usercustomize.py"})
IGNORED_DIRS = frozenset({".git"})


def _parts(relative: str | PurePosixPath) -> tuple[str, ...]:
    return tuple(part for part in PurePosixPath(str(relative).replace("\\", "/")).parts if part not in {"", "."})


def _normal(relative: str | PurePosixPath) -> str:
    return "/".join(_parts(relative))


def is_cache_path(relative: str | PurePosixPath) -> bool:
    return any(part in CACHE_DIRS or part.endswith(".egg-info") for part in _parts(relative))


def is_startup_hook(relative: str | PurePosixPath) -> bool:
    parts = _parts(relative)
    return bool(parts) and (parts[-1] in STARTUP_HOOKS or parts[-1].endswith(".pth"))


def _is_test_file(parts: tuple[str, ...]) -> bool:
    if any(part in TEST_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_grading_path(relative: str | PurePosixPath, editable: Collection[str] | None = None) -> bool:
    """True when `relative` belongs to grading and must match the baseline before the protocol runs."""
    parts = _parts(relative)
    if not parts:
        return False
    if is_cache_path(relative) or is_startup_hook(relative):
        return True
    if editable is not None and _normal(relative) in {_normal(item) for item in editable}:
        return False
    return _is_test_file(parts) or parts[-1] in SUPPORT_FILES


def grading_paths(root: Path, editable: Collection[str] | None = None) -> set[str]:
    """Relative POSIX paths of grading files (and symlinks) under root. `.git` is skipped."""
    found: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        name = relative.as_posix()
        if is_grading_path(name, editable):
            found.add(name)
    return found
