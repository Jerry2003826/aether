import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_status_does_not_claim_codegraph_enabled() -> None:
    status = json.loads((ROOT / "STATUS.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "config" / "codegraph.lock.json").read_text(encoding="utf-8"))
    example = yaml.safe_load((ROOT / "config" / "codegraph.example.yaml").read_text(encoding="utf-8"))
    assert status["readiness"]["codegraph_enabled"] == "not_run"
    assert status["f15"]["product_enabled"] is False
    assert lock["product_enabled"] is False
    assert example["product_enabled"] is False
    assert example["security"]["telemetry_env"]["CODEGRAPH_TELEMETRY"] == "0"
