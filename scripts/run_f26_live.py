"""Write the F26 live probe result. Does not mark the task done."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))

from aether_worker.live_harness import LIVE_KEY, redact_secret, run_live_probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_live_probe()
    secret = os.environ.get(LIVE_KEY) or ""
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    text = redact_secret(text, secret)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return {"probed": 0, "failed": 1, "blocked": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
