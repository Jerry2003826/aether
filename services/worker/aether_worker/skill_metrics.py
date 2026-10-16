"""Finite skill metrics. A zero baseline cost is not turned into infinity."""

from __future__ import annotations

import math
from typing import Any

from aether_worker.skill_eval import SkillEvalError

_Z = 1.959963984540054


def assert_finite_costs(candidate_cost: int, baseline_cost: int) -> None:
    """Reject a ratio that cannot be a finite number before any container starts."""
    candidate = _cost(candidate_cost)
    baseline = _cost(baseline_cost)
    if baseline == 0 and candidate != 0:
        raise SkillEvalError("non_finite")
    if baseline != 0 and not math.isfinite(candidate / baseline):
        raise SkillEvalError("non_finite")


def metrics_from_grades(
    grades: list[dict[str, Any]],
    *,
    baseline_passes: int,
    candidate_cost: int,
    baseline_cost: int,
) -> dict[str, Any]:
    if isinstance(baseline_passes, bool) or not isinstance(baseline_passes, int) or baseline_passes < 0:
        raise SkillEvalError("non_finite")
    candidate = _cost(candidate_cost)
    baseline = _cost(baseline_cost)
    if baseline == 0:
        if candidate != 0:
            raise SkillEvalError("non_finite")
        cost_ratio = 1.0
    else:
        cost_ratio = candidate / baseline
        if not math.isfinite(cost_ratio):
            raise SkillEvalError("non_finite")
    families = len(grades)
    if baseline_passes > families:
        raise SkillEvalError("non_finite")
    passes = 0
    security = False
    for grade in grades:
        if grade.get("sentinel_visible") or grade.get("credential_present"):
            security = True
        elif grade.get("verdict") == "PASS":
            passes += 1
    if families == 0:
        success_delta = 0.0
        ci_lower = 0.0
    else:
        base_rate = baseline_passes / families
        success_delta = passes / families - base_rate
        ci_lower = _wilson_lower(passes, families) - base_rate
    if not math.isfinite(success_delta) or not math.isfinite(ci_lower) or not math.isfinite(cost_ratio):
        raise SkillEvalError("non_finite")
    return {
        "families": families,
        "grade_passes": passes,
        "ci_lower": ci_lower,
        "success_delta": success_delta,
        "cost_ratio": cost_ratio,
        "security_failure": security,
    }


def _cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SkillEvalError("non_finite")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SkillEvalError("non_finite")
    return number


def _wilson_lower(passes: int, total: int) -> float:
    phat = passes / total
    z2 = _Z * _Z
    denom = 1 + z2 / total
    centre = (phat + z2 / (2 * total)) / denom
    margin = _Z * math.sqrt((phat * (1 - phat) + z2 / (4 * total)) / total) / denom
    return centre - margin
