import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_example_stays_off_while_the_lock_is_the_owner_switch() -> None:
    lock = json.loads((ROOT / "config" / "codegraph.lock.json").read_text(encoding="utf-8"))
    example = yaml.safe_load((ROOT / "config" / "codegraph.example.yaml").read_text(encoding="utf-8"))
    matrix = yaml.safe_load((ROOT / "config" / "feature_matrix.yaml").read_text(encoding="utf-8"))
    assert lock["product_enabled"] is True
    assert lock["telemetry_env"]["CODEGRAPH_TELEMETRY"] == "0"
    assert example["product_enabled"] is False
    assert example["security"]["telemetry_env"]["CODEGRAPH_TELEMETRY"] == "0"
    assert matrix["features"]["codegraph"]["enabled"] is False
    assert matrix["features"]["codegraph"]["owner_approval_required"] is True
