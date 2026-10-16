"""SYS-07: non-finite, mistyped, missing, and out-of-range metrics are not ELIGIBLE."""

from __future__ import annotations

import math
from typing import Any

import pytest

from aether_worker.skill_eval import judge

GOOD = {"families": 30, "ci_lower": 0.1, "success_delta": 0.1, "cost_ratio": 1.0}


def _replace(name: str, value: Any) -> dict[str, Any]:
    metrics = dict(GOOD)
    metrics[name] = value
    return metrics


def _without(name: str) -> dict[str, Any]:
    metrics = dict(GOOD)
    del metrics[name]
    return metrics


def test_finite_metrics_inside_range_stay_eligible() -> None:
    verdict = judge(GOOD, synthetic=False)
    assert verdict["decision"] == "ELIGIBLE"
    assert verdict["promoted"] is True
    assert verdict["learned"] is False


@pytest.mark.parametrize(
    "metrics",
    [
        _replace("ci_lower", "NaN"),
        _replace("success_delta", "NaN"),
        _replace("cost_ratio", "NaN"),
        _replace("families", "NaN"),
        _replace("ci_lower", math.nan),
        _replace("success_delta", math.nan),
        _replace("cost_ratio", math.nan),
        _replace("families", math.nan),
        _replace("ci_lower", math.inf),
        _replace("success_delta", math.inf),
        _replace("cost_ratio", math.inf),
        _replace("ci_lower", -math.inf),
        _replace("success_delta", -math.inf),
        _replace("cost_ratio", -math.inf),
        _replace("families", True),
        _replace("ci_lower", True),
        _replace("success_delta", True),
        _replace("cost_ratio", True),
        _replace("cost_ratio", False),
        _replace("families", False),
        _without("families"),
        _without("ci_lower"),
        _without("success_delta"),
        _without("cost_ratio"),
        _replace("ci_lower", None),
        _replace("families", None),
        _replace("success_delta", None),
        _replace("cost_ratio", None),
        _replace("families", -1),
        _replace("cost_ratio", -0.01),
        _replace("ci_lower", 1.01),
        _replace("ci_lower", -1.01),
        _replace("success_delta", 1.01),
        _replace("success_delta", -1.01),
        _replace("families", "30"),
        _replace("ci_lower", "0.2"),
        _replace("success_delta", "0.2"),
        _replace("cost_ratio", "1"),
        _replace("families", 30.0),
    ],
    ids=[
        "ci_lower_nan_string",
        "delta_nan_string",
        "cost_nan_string",
        "families_nan_string",
        "ci_lower_nan",
        "delta_nan",
        "cost_nan",
        "families_nan",
        "ci_lower_inf",
        "delta_inf",
        "cost_inf",
        "ci_lower_neginf",
        "delta_neginf",
        "cost_neginf",
        "families_true",
        "ci_lower_true",
        "delta_true",
        "cost_true",
        "cost_false",
        "families_false",
        "families_missing",
        "ci_lower_missing",
        "delta_missing",
        "cost_missing",
        "ci_lower_none",
        "families_none",
        "delta_none",
        "cost_none",
        "families_negative",
        "cost_negative",
        "ci_lower_above_one",
        "ci_lower_below_minus_one",
        "delta_above_one",
        "delta_below_minus_one",
        "families_digit_string",
        "ci_lower_digit_string",
        "delta_digit_string",
        "cost_digit_string",
        "families_float",
    ],
)
def test_invalid_metrics_are_never_eligible(metrics: dict[str, Any]) -> None:
    verdict = judge(metrics, synthetic=False)
    assert verdict["decision"] != "ELIGIBLE"
    assert verdict["promoted"] is False
    assert verdict["learned"] is False


def test_threshold_failures_stay_ineligible() -> None:
    few = judge(_replace("families", 3), synthetic=False)
    assert few["decision"] == "INSUFFICIENT_EVIDENCE"
    assert few["promoted"] is False
    low_ci = judge(_replace("ci_lower", -0.01), synthetic=False)
    assert low_ci["promoted"] is False
    assert low_ci["decision"] != "ELIGIBLE"
    weak = judge(_replace("success_delta", 0.01), synthetic=False)
    assert weak["decision"] == "REJECTED"
    assert weak["promoted"] is False
    costly = judge(_replace("cost_ratio", 1.2), synthetic=False)
    assert costly["decision"] == "REJECTED"
    assert costly["promoted"] is False
    unsafe = judge({**GOOD, "security_failure": True}, synthetic=False)
    assert unsafe["decision"] == "REJECTED"
    assert unsafe["promoted"] is False
    small = judge({**GOOD, "families": 1, "security_failure": True}, synthetic=False)
    assert small["decision"] == "REJECTED"
    assert small["promoted"] is False


def test_synthetic_control_stays_rejected() -> None:
    verdict = judge({**GOOD, "ci_lower": "NaN"}, synthetic=True)
    assert verdict["decision"] == "REJECTED"
    assert verdict["label"] == "synthetic_control"
    assert verdict["promoted"] is False
    assert verdict["learned"] is False
