"""Real CodeGraph evidence against one git snapshot. No fabricated query results."""

from __future__ import annotations

import ast
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from aether_worker.codegraph_adapter import SKIP_DIRECTORIES, CodeGraphAdapter, CodeGraphError

PROJECT_ID = "00000000-0000-4000-8000-0000000000d1"
REPOSITORY_ID = "00000000-0000-4000-8000-0000000000d2"
SNAPSHOT_ID = "repo-snapshot"
CALL = {
    "caller": "build_report",
    "caller_file": "services/worker/aether_worker/acceptance_report.py",
    "callee": "_counts",
    "callee_file": "services/worker/aether_worker/acceptance_report.py",
}
IMPORT = {
    "file": "services/worker/aether_worker/f26_acceptance.py",
    "module": "aether_worker.live_harness",
    "name": "redact_secret",
}


def export_git_snapshot(root: Path, destination: Path) -> int:
    completed = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError("git_ls_files_failed")
    copied = 0
    for name in completed.stdout.decode().split("\0"):
        if not name:
            continue
        relative = Path(name)
        if any(part in SKIP_DIRECTORIES or part.startswith(".env") for part in relative.parts):
            continue
        source = root / relative
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        copied += 1
    return copied


def _functions(tree: ast.AST) -> list[tuple[str, int, ast.AST]]:
    found: list[tuple[str, int, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((node.name, node.lineno, node))
    return found


def ast_calls(root: Path) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """Map (file, function) to the project functions it calls by name."""
    defined: dict[str, set[str]] = {}
    trees: list[tuple[str, ast.AST]] = []
    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root)
        if not path.is_file() or path.is_symlink() or any(part in SKIP_DIRECTORIES for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        try:
            parsed = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        trees.append((relative, parsed))
        for name, _line, _node in _functions(parsed):
            defined.setdefault(name, set()).add(relative)
    calls: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for relative, parsed in trees:
        for name, _line, node in _functions(parsed):
            callees: set[tuple[str, str]] = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                    for callee_file in defined.get(inner.func.id, set()):
                        callees.add((callee_file, inner.func.id))
            calls[(relative, name)] = callees
    return calls


def ast_imports(root: Path, filename: str) -> set[tuple[str, str]]:
    path = root / filename
    parsed = ast.parse(path.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found.add((node.module, alias.name))
    return found


def _call_rows(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(row["relative_path"]), str(row["name"]))
        for row in rows
        if row.get("kind") == "function" and "calls" in row.get("relationships", [])
    }


def edge_agrees(calls: dict[tuple[str, str], set[tuple[str, str]]], caller_file: str, caller: str, callee_file: str, callee: str, caller_rows: list[dict[str, Any]], callee_rows: list[dict[str, Any]]) -> dict[str, Any]:
    expected_callees = calls.get((caller_file, caller), set())
    expected_callers = {pair for pair, targets in calls.items() if (callee_file, callee) in targets}
    observed_callees = _call_rows(callee_rows)
    observed_callers = _call_rows(caller_rows)
    callee_hit = (callee_file, callee) in observed_callees
    caller_hit = (caller_file, caller) in observed_callers
    extra_callees = sorted(f"{path}:{name}" for path, name in observed_callees - expected_callees)
    extra_callers = sorted(f"{path}:{name}" for path, name in observed_callers - expected_callers)
    return {
        "callee_hit": callee_hit,
        "caller_hit": caller_hit,
        "extra_callees": extra_callees,
        "extra_callers": extra_callers,
        "ok": callee_hit and caller_hit and not extra_callees and not extra_callers,
    }


def collect_evidence(adapter: CodeGraphAdapter, source: Path, *, git_commit: str | None) -> dict[str, Any]:
    bound = adapter.bind(PROJECT_ID, REPOSITORY_ID, SNAPSHOT_ID, source)
    snapshot = Path(bound["path"])
    outside = adapter.home / "outside-secret.txt"
    outside.write_text("UNIQUE_OUTSIDE_TOKEN\n", encoding="utf-8")
    before = adapter.fallback(PROJECT_ID, SNAPSHOT_ID, "def build_report")
    marker = "def _counts"
    degraded = adapter.fallback(PROJECT_ID, SNAPSHOT_ID, marker)
    started = time.perf_counter()
    indexed = adapter.index(PROJECT_ID, SNAPSHOT_ID)
    index_seconds = time.perf_counter() - started
    status = adapter.index_status(PROJECT_ID, SNAPSHOT_ID)
    calls = ast_calls(snapshot)
    callers = adapter.callers(PROJECT_ID, SNAPSHOT_ID, CALL["callee"], relative_file=CALL["callee_file"])
    callees = adapter.callees(PROJECT_ID, SNAPSHOT_ID, CALL["caller"], relative_file=CALL["caller_file"])
    agreement = edge_agrees(
        calls,
        CALL["caller_file"],
        CALL["caller"],
        CALL["callee_file"],
        CALL["callee"],
        callers["rows"],
        callees["rows"],
    )
    imports = ast_imports(snapshot, IMPORT["file"])
    import_rows = adapter.callers(PROJECT_ID, SNAPSHOT_ID, IMPORT["name"], relative_file="services/worker/aether_worker/live_harness.py")
    import_hit = any(
        row["relative_path"] == IMPORT["file"] and "imports" in row["relationships"]
        for row in import_rows["rows"]
    )
    import_expected = (IMPORT["module"], IMPORT["name"]) in imports
    (snapshot / CALL["caller_file"]).write_text("def build_report():\n    return None\n", encoding="utf-8")
    stale_code = ""
    try:
        adapter.callers(PROJECT_ID, SNAPSHOT_ID, CALL["callee"], relative_file=CALL["callee_file"])
    except CodeGraphError as exc:
        stale_code = exc.code
    checks = {
        "index": status.get("state") == "complete" and int(status.get("file_count") or 0) > 0 and indexed["cold_index_seconds"] > 0,
        "calls": agreement["ok"] and not callers["ambiguous"] and not callees["ambiguous"],
        "dependency": import_expected and import_hit and not import_rows["ambiguous"],
        "invalidation": stale_code == "stale_snapshot",
        "degraded": degraded["index_status"] == "degraded" and degraded["degraded_reason"] == "graph_unavailable" and CALL["caller_file"] in degraded["matches"] and "outside-secret.txt" not in degraded["matches"] and before["degraded_reason"] == "graph_unavailable",
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "product_enabled": adapter.product_enabled,
        "telemetry": "off",
        "checks": checks,
        "copied_files": sum(1 for path in snapshot.rglob("*") if path.is_file() and ".codegraph" not in path.parts),
        "index": {
            "cold_index_seconds": index_seconds,
            "reported_cold_index_seconds": indexed["cold_index_seconds"],
            "file_count": status.get("file_count"),
            "node_count": status.get("node_count"),
            "edge_count": status.get("edge_count"),
            "db_size_bytes": status.get("db_size_bytes"),
            "state": status.get("state"),
            "version": status.get("version"),
            "cache_key": bound["cache_key"],
            "snapshot_sha256": bound["snapshot_sha256"],
        },
        "call": {**CALL, "agreement": agreement, "callers": callers["rows"], "callees": callees["rows"]},
        "dependency": {**IMPORT, "ast_import": import_expected, "graph_import": import_hit, "rows": import_rows["rows"]},
        "invalidation": {"code": stale_code},
        "degraded": {"reason": degraded["degraded_reason"], "matches": degraded["matches"][:20]},
        "network_none": all("--network" in item and item[item.index("--network") + 1] == "none" for item in adapter.invocations),
        "git_commit": git_commit,
    }
