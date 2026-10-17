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
    is_shadow_module(rel)                True when rel is (inside) a top-level module or package whose
                                         name is in shadow_names(). SANDBOX ONLY, see below. An agent
                                         that ADDS one fails the job with grading_shadow_module.

shadow_names() / is_shadow_module() reflect the interpreter they run in: the stdlib and builtin
module names of that interpreter, the top-level packages installed in its environment, and
SHADOW_ROOTS (mirrors verification._SHADOW_ROOTS). They must only be called inside the sandbox
(openhands_sandbox_entry.py, running in the sandbox image), where that interpreter is the one
that grades. Worker-side code must not call them, because the worker environment has a different
package set; the worker only consumes the grading_shadow_module case detail.
    grading_paths(root, editable=None)   relative POSIX paths of grading files under root (.git skipped).
"""

from __future__ import annotations

import sys
from collections.abc import Collection
from functools import cache
from importlib import metadata
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
# Grading runs the stdlib coding_protocol on test_*.py / *_test.py anywhere in the tree and never
# pytest (pytest is not installed in the sandbox image), so no pytest config is read at grading time. Every config is still restored
# from the baseline or deleted, so a pytest-based grader could not be redirected by an added one.
SUPPORT_FILES = frozenset(
    {
        "oracle.py",
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
    }
)
STARTUP_HOOKS = frozenset({"sitecustomize.py", "usercustomize.py"})
IGNORED_DIRS = frozenset({".git"})
# Mirrors aether_worker.verification._SHADOW_ROOTS (F14), as import names. Kept even where the
# package is not installed (pytest is not in the sandbox image) so a later grader is covered.
SHADOW_ROOTS = frozenset({"pytest", "_pytest", "pluggy", "sitecustomize", "usercustomize"})
_EXTENSION_SUFFIXES = (".py", ".pyc", ".so", ".pyd")


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


def _installed_top_level() -> frozenset[str]:
    """Top-level import names installed in the running interpreter's environment (the sandbox image)."""
    try:
        return frozenset(metadata.packages_distributions())
    except Exception:  # noqa: BLE001 - a broken dist-info must not disable the check
        return frozenset()


@cache
def shadow_names() -> frozenset[str]:
    """Import names an added top-level module must not take. SANDBOX ONLY: reflects the running interpreter.

    = packages installed in this interpreter's environment + its stdlib/builtin modules + SHADOW_ROOTS.
    """
    return SHADOW_ROOTS | frozenset(sys.stdlib_module_names) | frozenset(sys.builtin_module_names) | _installed_top_level()


def _top_level_name(parts: tuple[str, ...]) -> str | None:
    if len(parts) > 1:
        return parts[0]
    name = parts[0]
    if name.endswith(_EXTENSION_SUFFIXES):
        return name.split(".", 1)[0]
    return None


def is_shadow_module(relative: str | PurePosixPath) -> bool:
    """SANDBOX ONLY (see shadow_names). True for a top-level module/package that would shadow an import."""
    parts = _parts(relative)
    if not parts:
        return False
    top = _top_level_name(parts)
    return top is not None and top in shadow_names()


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
