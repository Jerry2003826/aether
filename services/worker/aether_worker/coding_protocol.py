"""Stdlib test protocol executed by OpenHands inside the sandbox. It does not score or import OpenHands.

Integrity: before any candidate code runs, every Python source and grading file under the workspace
(virtualenvs with pyvenv.cfg, node_modules, VCS and cache dirs skipped) is read into memory and hashed. All Python imports from the workspace (test modules, oracle.py, candidate code, packages)
are served from that in-memory snapshot, so a file rewritten during the run has no effect on any
module loaded afterwards. Those files are hashed again before each test module and after the run;
one that is missing or changed fails the suite with `grading_tampered`. New files are fine.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

# grading_files.py ships next to this script (repo and image); `-I` drops the script dir from sys.path.
# Appended, then removed, so it cannot shadow any other module.
_HERE = str(Path(__file__).resolve().parent)
sys.path.append(_HERE)
try:
    import grading_files
finally:
    sys.path.remove(_HERE)

# Never walked: VCS data, caches, virtualenvs and vendored dependency trees.
_SKIP_DIRS = frozenset(
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
_VENV_DIRS = frozenset({".venv", "venv", "env"})


def _skipped(path: Path) -> bool:
    if path.name in _SKIP_DIRS:
        return True
    return path.name in _VENV_DIRS and (path / "pyvenv.cfg").is_file()


def _guarded(name: str) -> bool:
    """Snapshot and verify Python sources and grading files; other data may change freely."""
    return name.endswith(".py") or grading_files.is_grading_path(name)


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
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
                if not _skipped(path):
                    pending.append(path)
                continue
            name = path.relative_to(root).as_posix()
            if path.is_file() and _guarded(name):
                found[name] = path
    return found


def _tampered(root: Path, baseline: dict[str, str]) -> bool:
    """A snapshotted file that is now missing or different. New files are fine."""
    for name, digest in baseline.items():
        try:
            data = (root / name).read_bytes()
        except OSError:
            return True
        if (root / name).is_symlink() or hashlib.sha256(data).hexdigest() != digest:
            return True
    return False


class _SnapshotLoader(importlib.abc.Loader):
    def __init__(self, source: bytes, origin: str) -> None:
        self._source = source
        self._origin = origin

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        code = compile(self._source, self._origin, "exec", dont_inherit=True)
        exec(code, module.__dict__)  # noqa: S102 - running the suite is the point


class _SnapshotFinder(importlib.abc.MetaPathFinder):
    """Serves workspace modules from bytes captured before any candidate code ran."""

    def __init__(self, root: Path, sources: dict[str, bytes]) -> None:
        self._root = root
        self._modules: dict[str, tuple[str, bytes, bool]] = {}
        for name, data in sources.items():
            if not name.endswith(".py"):
                continue
            parts = name[:-3].split("/")
            package = parts[-1] == "__init__"
            if package:
                parts = parts[:-1]
            if not parts or not all(part.isidentifier() for part in parts):
                continue
            self._modules[".".join(parts)] = (name, data, package)

    def find_spec(self, fullname, path=None, target=None):
        entry = self._modules.get(fullname)
        if entry is None:
            return None
        name, data, package = entry
        origin = str(self._root / name)
        spec = importlib.machinery.ModuleSpec(fullname, _SnapshotLoader(data, origin), origin=origin, is_package=package)
        if package:
            spec.submodule_search_locations = [str((self._root / name).parent)]
        spec.has_location = True
        return spec


def run_suite(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    cases: list[dict[str, str]] = []
    passed = 0
    failed = 0
    sys.dont_write_bytecode = True
    files = _files(root)
    sources = {name: path.read_bytes() for name, path in files.items()}
    baseline = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    tests = sorted(path.name for path in root.glob("test_*.py") if path.name in sources)
    sys.meta_path.insert(0, _SnapshotFinder(root, sources))
    sys.path.insert(0, str(root))
    tampered = False
    for name in tests:
        if _tampered(root, baseline):
            tampered = True
            break
        stem = name[:-3]
        spec = importlib.util.find_spec(stem)
        if spec is None or spec.loader is None:
            failed += 1
            cases.append({"name": stem, "verdict": "FAIL", "detail": "import"})
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[stem] = module
        spec.loader.exec_module(module)
        for attr in sorted(dir(module)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(module, attr)
            if not callable(fn):
                continue
            try:
                value = fn()
            except Exception as exc:
                failed += 1
                cases.append({"name": stem + "." + attr, "verdict": "FAIL", "detail": type(exc).__name__})
                continue
            if inspect.iscoroutine(value):
                value.close()
                failed += 1
                cases.append({"name": stem + "." + attr, "verdict": "FAIL", "detail": "async"})
                continue
            passed += 1
            cases.append({"name": stem + "." + attr, "verdict": "PASS", "detail": ""})
    if tampered or _tampered(root, baseline):
        failed += 1
        cases.append({"name": "suite", "verdict": "FAIL", "detail": "grading_tampered"})
    if not cases:
        failed += 1
        cases.append({"name": "suite", "verdict": "FAIL", "detail": "no_tests"})
    return {
        "passed": passed,
        "failed": failed,
        "errors": 0,
        "cases": cases,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def main() -> None:
    body = run_suite(Path.cwd())
    sys.stdout.write(json.dumps(body, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    raise SystemExit(0 if body["failed"] == 0 and body["passed"] > 0 else 1)


if __name__ == "__main__":
    main()
