"""Hermes comparison driver. Missing CLI or model key stays not_run."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

GROUPS = ("hermes_cold", "hermes_learned", "aether_cold", "aether_learned")
LIVE_KEY = "AETHER_LIVE_MODEL_KEY"
SHARED_BUDGET = {"max_cost_micro_usd": 2_000_000, "max_output_tokens": 100_000, "model_alias": "actor"}
UNMATCHED = [
    "Hermes 使用独立 home，AETHER 使用项目数据库。",
    "没有同一条已安装的 Hermes CLI 时，不能把两边写成相同的运行时。",
]


def probe_cli(binary: str | None = None) -> dict[str, Any]:
    path = binary or shutil.which("hermes")
    if not path:
        return {"status": "not_run", "reason": "hermes_cli_missing", "version": None, "path": None}
    completed = subprocess.run([path, "--version"], capture_output=True, text=True, check=False, timeout=90)
    version = completed.stdout.strip() or completed.stderr.strip()
    return {
        "status": "probed" if completed.returncode == 0 and version else "failed",
        "reason": None if completed.returncode == 0 and version else "version_probe_failed",
        "version": version or None,
        "path": path,
        "returncode": completed.returncode,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def load_dev_cases(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(row.get("split") == "holdout" for row in rows):
        raise ValueError("holdout_in_dev_file")
    return {"count": len(rows), "sha256": _sha256(path), "split": "public_development"}


def holdout_status(path: Path | None, *, repo_root: Path) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"status": "not_run", "reason": "holdout_not_supplied"}
    try:
        path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return {"status": "sealed", "reason": "outside_worktree", "sha256": _sha256(path)}
    return {"status": "rejected", "reason": "holdout_inside_worktree"}


def experiment_plan(dev_cases: Path, *, repo_root: Path, holdout: Path | None = None) -> dict[str, Any]:
    key_present = bool(os.environ.get(LIVE_KEY))
    cli = probe_cli()
    return {
        "groups": [
            {"name": name, "budget": dict(SHARED_BUDGET), "home": f"isolated-{name}", "learned": name.endswith("learned")}
            for name in GROUPS
        ],
        "dev": load_dev_cases(dev_cases),
        "holdout": holdout_status(holdout, repo_root=repo_root),
        "live": "not_run" if not key_present or cli["status"] != "probed" else "ready_to_run",
        "missing_live_key": None if key_present else LIVE_KEY,
        "cli": cli,
        "unmatched": list(UNMATCHED),
        "superior": None,
    }


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float] | None:
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def summarize(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    report: dict[str, Any] = {"groups": {}, "superior": None, "unknown_costs_dropped": False}
    comparable = True
    for name in GROUPS:
        rows = groups.get(name) or []
        successes = sum(1 for row in rows if row.get("success") is True)
        unknown = sum(1 for row in rows if row.get("cost_status") == "unknown")
        known = [int(row["cost_micro_usd"]) for row in rows if row.get("cost_status") == "known"]
        interval = wilson_interval(successes, len(rows))
        report["groups"][name] = {
            "n": len(rows),
            "successes": successes,
            "unknown_cost_count": unknown,
            "known_cost_micro_usd": known,
            "cost_mean_micro_usd": None if unknown or not rows else sum(known) / len(rows),
            "wilson95": None if interval is None else [interval[0], interval[1]],
        }
        if not rows or unknown:
            comparable = False
    if comparable:
        report["superior"] = None
        report["reason"] = "四组都有原始计数，但没有冻结实验证明某一组领先。"
    else:
        report["reason"] = "有组没有运行，或有 unknown 费用。不计算删掉这些行之后的均值，也不宣布领先。"
    return report
