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
    """Serves workspace modules from bytes captured before any candidate code ran.

    Names resolve against the workspace root first, then against each non-package test directory
    (so a test there can import a sibling helper by bare name, as with pytest's default rootdir mode).
    """

    def __init__(self, root: Path, sources: dict[str, bytes], bases: list[str]) -> None:
        self._root = root
        self._modules: dict[str, tuple[str, bytes, bool]] = {}
        for base in ["", *bases]:
            prefix = base + "/" if base else ""
            for name, data in sources.items():
                if not name.endswith(".py") or not name.startswith(prefix):
                    continue
                parts = name[len(prefix) : -3].split("/")
                package = parts[-1] == "__init__"
                if package:
                    parts = parts[:-1]
                if not parts or not all(part.isidentifier() for part in parts):
                    continue
                self._modules.setdefault(".".join(parts), (name, data, package))

    def find_spec(self, fullname, path=None, target=None):
        entry = self._modules.get(fullname)
        if entry is None:
            return None
        name, data, package = entry
        return _spec(fullname, self._root / name, data, package)


def _spec(fullname: str, origin_path: Path, data: bytes, package: bool = False):
    origin = str(origin_path)
    spec = importlib.machinery.ModuleSpec(fullname, _SnapshotLoader(data, origin), origin=origin, is_package=package)
    if package:
        spec.submodule_search_locations = [str(origin_path.parent)]
    spec.has_location = True
    return spec


def _is_test_module(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def _test_modules(sources: dict[str, bytes]) -> list[tuple[str, str, str]]:
    """(relative path, import name or "" for a direct load, base dir) of every test module, root first."""
    found: list[tuple[str, str, str]] = []
    for name in sources:
        if not _is_test_module(name):
            continue
        parts = name[:-3].split("/")
        if not all(part.isidentifier() for part in parts):
            continue
        # Climb while the parent directory is a package; the first non-package directory is the base.
        depth = len(parts) - 1
        while depth > 0 and "/".join(parts[:depth]) + "/__init__.py" in sources:
            depth -= 1
        base = "/".join(parts[:depth])
        # A package under a non-package base is imported through the finder (relative imports work);
        # a plain module there is loaded directly under its full dotted path.
        importable = depth == 0 or depth < len(parts) - 1
        found.append((name, ".".join(parts[depth:]) if importable else "", base))
    return sorted(found, key=lambda item: (item[0].count("/"), item[0]))


def run_suite(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    cases: list[dict[str, str]] = []
    passed = 0
    failed = 0
    sys.dont_write_bytecode = True
    files = _files(root)
    sources = {name: path.read_bytes() for name, path in files.items()}
    baseline = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    tests = _test_modules(sources)
    bases = sorted({base for _, _, base in tests if base})
    sys.meta_path.insert(0, _SnapshotFinder(root, sources, bases))
    sys.path.insert(0, str(root))
    sys.path[1:1] = [str(root / base) for base in bases]
    tampered = False
    for relative, import_name, _base in tests:
        if _tampered(root, baseline):
            tampered = True
            break
        label = relative[:-3].replace("/", ".")
        try:
            if import_name:
                module = importlib.import_module(import_name)
            else:
                # Loaded straight from the snapshot under its full dotted path, so same-named test files
                # in different directories never hide each other.
                module = importlib.util.module_from_spec(_spec(label, root / relative, sources[relative]))
                sys.modules[label] = module
                module.__spec__.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - any candidate failure is a FAIL verdict
            failed += 1
            cases.append(
                {
                    "name": label,
                    "verdict": "FAIL",
                    "detail": "import:" + type(exc).__name__,
                }
            )
            continue
        for attr in sorted(dir(module)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(module, attr)
            if not callable(fn):
                continue
            try:
                value = fn()
            except Exception as exc:  # noqa: BLE001 - any candidate failure is a FAIL verdict
                failed += 1
                cases.append(
                    {
                        "name": label + "." + attr,
                        "verdict": "FAIL",
                        "detail": type(exc).__name__,
                    }
                )
                continue
            if inspect.iscoroutine(value):
                value.close()
                failed += 1
                cases.append({"name": label + "." + attr, "verdict": "FAIL", "detail": "async"})
                continue
            passed += 1
            cases.append({"name": label + "." + attr, "verdict": "PASS", "detail": ""})
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
