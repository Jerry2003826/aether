"""Supervisor entry for the full F26 acceptance. Does not mark the task done."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))
sys.path.insert(0, str(ROOT / "packages" / "storage"))
sys.path.append(str(ROOT))

from aether_worker.f26_acceptance import LIVE_KEY, RESULT_NAME, run_f26_acceptance
from aether_worker.live_harness import redact_secret

RESULT_PATH = ROOT / "reports" / RESULT_NAME
ALLOWED_OUT = Path("reports") / RESULT_NAME
_EVIDENCE_OUT = re.compile(r"^reports/f26_acceptance_result_([0-9a-f]{7,40})\.json$")


def _head() -> str | None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"], capture_output=True, text=True, env=env, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    head = done.stdout.strip()
    return head if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else None


def _destination(out: str) -> Path | None:
    """``reports/f26_acceptance_result.json``, or the evidence name for the current HEAD."""
    if Path(out) == ALLOWED_OUT:
        return RESULT_PATH
    match = _EVIDENCE_OUT.fullmatch(out)
    head = _head() if match else None
    if match is None or head is None or not head.startswith(match.group(1)):
        return None
    return ROOT / "reports" / f"f26_acceptance_result_{match.group(1)}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    destination = _destination(args.out)
    if destination is None:
        raise SystemExit("out must be reports/f26_acceptance_result.json")
    report = run_f26_acceptance()
    secret = os.environ.get(LIVE_KEY) or ""
    text = redact_secret(json.dumps(report, ensure_ascii=False, indent=2) + "\n", secret)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return {"probed": 0, "failed": 1, "blocked": 2, "incomplete": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
