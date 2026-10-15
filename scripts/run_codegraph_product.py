"""Index this git snapshot with the pinned CodeGraph CLI and write the evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))

from aether_worker.codegraph_adapter import CodeGraphAdapter, install_cli
from aether_worker.codegraph_evidence import collect_evidence, export_git_snapshot

RESULT_NAME = "f15_product_result.json"
RESULT_PATH = ROOT / "reports" / RESULT_NAME
ALLOWED_OUT = Path("reports") / RESULT_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if Path(args.out) != ALLOWED_OUT:
        raise SystemExit("out must be reports/f15_product_result.json")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
    with tempfile.TemporaryDirectory(prefix="aether-codegraph-") as temporary:
        work = Path(temporary)
        source = work / "snapshot"
        source.mkdir()
        export_git_snapshot(ROOT, source)
        binary = install_cli(work / "prefix")
        adapter = CodeGraphAdapter(binary, work / "home")
        commit = head.stdout.strip() if head.returncode == 0 else None
        report = collect_evidence(adapter, source, git_commit=commit)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(RESULT_PATH.read_text(encoding="utf-8"))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
