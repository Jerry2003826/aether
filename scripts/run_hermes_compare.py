"""Supervisor entry for the capped four-way comparison. Does not mark it passed."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))

from aether_worker.hermes_compare import RESULT_NAME, run_four_way
from aether_worker.live_harness import LIVE_KEY, redact_secret

RESULT_PATH = ROOT / "reports" / RESULT_NAME
ALLOWED_OUT = Path("reports") / RESULT_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if Path(args.out) != ALLOWED_OUT:
        raise SystemExit("out must be reports/hermes_compare_result.json")
    raw = os.environ.get("AETHER_HOLDOUT_PATH")
    holdout = Path(raw) if raw else None
    report = run_four_way(holdout=holdout)
    secret = os.environ.get(LIVE_KEY) or ""
    text = redact_secret(json.dumps(report, ensure_ascii=False, indent=2) + "\n", secret)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return {"probed": 0, "failed": 1, "blocked": 2, "incomplete": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
