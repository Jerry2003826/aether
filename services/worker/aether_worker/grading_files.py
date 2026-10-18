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
    grading_paths(root, editable=None, marked=frozenset())
                                         relative POSIX paths of grading files under root (.git skipped).
    marked_grading_files(baseline)       root-level .py files of the BASELINE whose first line starts with
                                         GRADING_MARKER: grading helpers (threat model I-1, option 2).
                                         Always computed from the baseline, never from the agent's copy;
                                         pass the result as `marked` to is_grading_path/grading_paths.
    declared_tests(source)               test ids a test module declares (ast, baseline source).
    grading_steps(root)                  processes the protocol will start; sizes its time limit.
    protocol_timeout_seconds(steps)      the protocol's limit: base + per step, capped (ADR-F44).
"""

from __future__ import annotations

import ast
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
# Grading (coding_protocol) runs pytest on test_*.py / *_test.py anywhere in the tree with an empty
# config (-c /dev/null), --noconftest and plugin autoload off, so no workspace pytest config or conftest
# is read at grading time. Every config is still restored from the baseline or deleted as well.
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
# Mirrors aether_worker.verification._SHADOW_ROOTS (F14), as import names. pytest is installed in the
# sandbox image (the grader runs it); kept here even where it is not installed.
SHADOW_ROOTS = frozenset({"pytest", "_pytest", "pluggy", "sitecustomize", "usercustomize"})
_EXTENSION_SUFFIXES = (".py", ".pyc", ".so", ".pyd")
# First line of a root-level helper the task owner marks as grading (followed by end of line or spaces).
GRADING_MARKER = b"# aether: grading"


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


def is_grading_path(
    relative: str | PurePosixPath, editable: Collection[str] | None = None, marked: Collection[str] = frozenset()
) -> bool:
    """True when `relative` belongs to grading and must match the baseline before the protocol runs.

    `marked` is marked_grading_files() of the baseline."""
    parts = _parts(relative)
    if not parts:
        return False
    if is_cache_path(relative) or is_startup_hook(relative):
        return True
    if editable is not None and _normal(relative) in {_normal(item) for item in editable}:
        return False
    return _is_test_file(parts) or parts[-1] in SUPPORT_FILES or _normal(relative) in marked


def _marked_first_line(head: bytes) -> bool:
    line = head.split(b"\n", 1)[0].rstrip(b"\r")
    return line.startswith(GRADING_MARKER) and not line[len(GRADING_MARKER) :].strip(b" \t")


def marked_grading_files(baseline: Path) -> frozenset[str]:
    """Root-level .py files of the baseline whose first line is GRADING_MARKER (regular files only)."""
    found: set[str] = set()
    try:
        entries = sorted(baseline.iterdir())
    except OSError:
        return frozenset()
    for path in entries:
        if not path.name.endswith(".py") or path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(len(GRADING_MARKER) + 256)
        except OSError:
            continue
        if _marked_first_line(head):
            found.add(path.name)
    return frozenset(found)


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


def grading_paths(root: Path, editable: Collection[str] | None = None, marked: Collection[str] = frozenset()) -> set[str]:
    """Relative POSIX paths of grading files (and symlinks) under root. `.git` is skipped."""
    found: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        name = relative.as_posix()
        if is_grading_path(name, editable, marked):
            found.add(name)
    return found


# ---------------------------------------------------------------------------------------------
# What the grading protocol collects, and how long it may take. Shared by coding_protocol.py (which
# runs each test module in its own process) and the sandbox entry (which bounds the protocol).

# Never walked: VCS data, caches, virtualenvs and vendored dependency trees.
WALK_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".aether-state",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        "site-packages",
    }
)
# Skipped only when they really are virtualenvs (pyvenv.cfg); otherwise they may be candidate packages.
VENV_DIRS = frozenset({".venv", "venv", "env"})

# Seconds. One call into candidate code < one test (the time a test process has for each test's reply)
# < the whole protocol, which is PROTOCOL_BASE_SECONDS + PROTOCOL_STEP_SECONDS per step (a step is one
# declared test, or one test module), capped at PROTOCOL_CAP_SECONDS. The protocol starts one test
# process per module (a pytest session, about 0.2 s) plus one per resume after a crash or timeout, so the
# step allowance covers isolation overhead with room for real work; a hung suite stops at the cap.
PROTOCOL_CALL_TIMEOUT_SECONDS = 15
PROTOCOL_TEST_TIMEOUT_SECONDS = 20
PROTOCOL_BASE_SECONDS = 30
PROTOCOL_STEP_SECONDS = 2
PROTOCOL_CAP_SECONDS = 240


def protocol_timeout_seconds(steps: int) -> int:
    return min(PROTOCOL_CAP_SECONDS, PROTOCOL_BASE_SECONDS + PROTOCOL_STEP_SECONDS * max(0, steps))


def walk_skipped(path: Path) -> bool:
    if path.name in WALK_SKIP_DIRS:
        return True
    return path.name in VENV_DIRS and (path / "pyvenv.cfg").is_file()


def is_test_module(relative: str) -> bool:
    base = relative.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    return ""


def _local_bases(node: ast.ClassDef, classes: dict[str, ast.ClassDef], seen: set[str]) -> list[ast.ClassDef]:
    found = [node]
    for base in node.bases:
        name = _dotted(base)
        if name in classes and name not in seen:
            seen.add(name)
            found.extend(_local_bases(classes[name], classes, seen))
    return found


def _is_case_class(node: ast.ClassDef, classes: dict[str, ast.ClassDef]) -> bool:
    """A unittest.TestCase subclass, as far as the module's own source shows."""
    for item in _local_bases(node, classes, {node.name}):
        if any(_dotted(base).rsplit(".", 1)[-1].endswith("TestCase") for base in item.bases):
            return True
    return False


def _opted_out(node: ast.ClassDef) -> bool:
    """`__test__ = False` in the class body, the documented way to say a class holds no tests."""
    for item in node.body:
        if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant) and item.value.value is False:
            if any(isinstance(target, ast.Name) and target.id == "__test__" for target in item.targets):
                return True
    return False


def declared_tests(source: bytes) -> tuple[list[str], dict[str, str]] | None:
    """Tests a test module declares, read from its baseline source (None when it does not parse).

    Returns (test ids, {id: FAIL detail} for test classes that cannot be run). An id is a top-level
    `test*` function (or one imported by name), or `Class.method` for each `test*` method, own or
    inherited from a class in the same module, of a `Test*` class or a unittest.TestCase subclass.
    A `Test*` class with its own __init__, and a test class nested in a test class, cannot be run and
    are listed as failures, never dropped. Fixed before any code runs, so nothing can hide a test.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    ids: set[str] = set()
    unsupported: dict[str, str] = {}
    for node in tree.body:
        # pytest's python_functions is `test`: testFailure is a test like test_ok.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            ids.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            ids.update(name for name in (alias.asname or alias.name for alias in node.names) if name.startswith("test"))
        elif isinstance(node, ast.ClassDef):
            case = _is_case_class(node, classes)
            if not (case or node.name.startswith("Test")) or _opted_out(node):
                continue
            chain = _local_bases(node, classes, {node.name})
            members = [item for owner in chain for item in owner.body]
            if not case and any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__" for item in members):
                unsupported[node.name] = "unsupported_test_class:init"
                continue
            for item in node.body:
                if isinstance(item, ast.ClassDef) and (item.name.startswith("Test") or _is_case_class(item, classes)):
                    unsupported[f"{node.name}.{item.name}"] = "unsupported_test_class:nested"
            for item in members:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    ids.add(f"{node.name}.{item.name}")
    return sorted(ids), unsupported


def grading_steps(root: Path) -> int:
    """Steps for the test modules under root: one per module and one per declared test. Used to size the
    protocol's time limit before it runs."""
    steps = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.is_symlink():
                continue
            if path.is_dir():
                if not walk_skipped(path):
                    pending.append(path)
                continue
            relative = path.relative_to(root).as_posix()
            if not (path.is_file() and is_test_module(relative)):
                continue
            steps += 1
            try:
                declared = declared_tests(path.read_bytes())
            except OSError:
                continue
            if declared is not None:
                steps += len(declared[0])
    return steps
