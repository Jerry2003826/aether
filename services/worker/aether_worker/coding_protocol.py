"""Stdlib test protocol executed by OpenHands inside the sandbox. It does not score or import OpenHands."""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any


def run_suite(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    cases: list[dict[str, str]] = []
    passed = 0
    failed = 0
    tests = sorted(path for path in root.glob("test_*.py") if path.is_file() and not path.is_symlink())
    sys.path.insert(0, str(root))
    for path in tests:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            failed += 1
            cases.append({"name": path.stem, "verdict": "FAIL", "detail": "import"})
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
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
                cases.append({"name": path.stem + "." + attr, "verdict": "FAIL", "detail": type(exc).__name__})
                continue
            if inspect.iscoroutine(value):
                value.close()
                failed += 1
                cases.append({"name": path.stem + "." + attr, "verdict": "FAIL", "detail": "async"})
                continue
            passed += 1
            cases.append({"name": path.stem + "." + attr, "verdict": "PASS", "detail": ""})
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
