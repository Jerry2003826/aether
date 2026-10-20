"""Finite skill metrics. A zero baseline cost is not turned into infinity.

Uplift is a paired comparison on the same task families: family ``i`` is graded
once with the baseline and once with the candidate. Significance is the exact
McNemar test on the discordant pairs (statsmodels); the interval is a paired
bootstrap of the per-family difference with a fixed seed (SciPy). Every pair is
kept in the result so the numbers can be recomputed.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy.stats import bootstrap
from statsmodels.stats.contingency_tables import mcnemar

from aether_worker.skill_eval import SkillEvalError

BOOTSTRAP_SEED = 20261021
BOOTSTRAP_RESAMPLES = 9999
CONFIDENCE_LEVEL = 0.95


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
    candidate_cost: int,
    baseline_cost: int,
    baseline_outcomes: Sequence[bool] | None = None,
    baseline_passes: int | None = None,
    family_names: Sequence[str] | None = None,
) -> dict[str, Any]:
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
    baseline_list = _baseline(baseline_outcomes, baseline_passes, families)
    if family_names is not None and (len(family_names) != families or not all(isinstance(name, str) for name in family_names)):
        raise SkillEvalError("non_finite")
    candidate_list: list[bool] = []
    security = False
    for grade in grades:
        if grade.get("sentinel_visible") or grade.get("credential_present"):
            security = True
            candidate_list.append(False)
        else:
            candidate_list.append(grade.get("verdict") == "PASS")
    passes = sum(candidate_list)
    baseline_only = sum(1 for base, cand in zip(baseline_list, candidate_list, strict=True) if base and not cand)
    candidate_only = sum(1 for base, cand in zip(baseline_list, candidate_list, strict=True) if cand and not base)
    if families == 0:
        success_delta = 0.0
        ci_lower = ci_upper = 0.0
        p_value = 1.0
    else:
        success_delta = (passes - sum(baseline_list)) / families
        p_value = _mcnemar_exact(baseline_only, candidate_only)
        ci_lower, ci_upper = _paired_bootstrap(baseline_list, candidate_list)
    values = (success_delta, ci_lower, ci_upper, p_value, cost_ratio)
    if not all(math.isfinite(value) for value in values):
        raise SkillEvalError("non_finite")
    paired = [
        {
            "index": index,
            "family": None if family_names is None else family_names[index],
            "baseline": base,
            "candidate": cand,
        }
        for index, (base, cand) in enumerate(zip(baseline_list, candidate_list, strict=True))
    ]
    return {
        "families": families,
        "grade_passes": passes,
        "baseline_passes": sum(baseline_list),
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "success_delta": success_delta,
        "cost_ratio": cost_ratio,
        "security_failure": security,
        "significance_test": "mcnemar_exact",
        "p_value": p_value,
        "discordant": {"baseline_only": baseline_only, "candidate_only": candidate_only},
        "ci_method": "paired_bootstrap",
        "confidence_level": CONFIDENCE_LEVEL,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "paired": paired,
    }


def _baseline(outcomes: Sequence[bool] | None, passes: int | None, families: int) -> list[bool]:
    """Per-family baseline results. A bare count only pairs when it is 0 or all families."""
    if outcomes is not None:
        if len(outcomes) != families or not all(isinstance(item, bool) for item in outcomes):
            raise SkillEvalError("non_finite")
        if passes is not None and passes != sum(outcomes):
            raise SkillEvalError("non_finite")
        return list(outcomes)
    if passes is None or isinstance(passes, bool) or not isinstance(passes, int) or passes < 0 or passes > families:
        raise SkillEvalError("non_finite")
    if passes == 0:
        return [False] * families
    if passes == families:
        return [True] * families
    raise SkillEvalError("baseline_unpaired")


def _mcnemar_exact(baseline_only: int, candidate_only: int) -> float:
    if baseline_only + candidate_only == 0:
        return 1.0
    table = [[0, baseline_only], [candidate_only, 0]]
    return float(min(1.0, mcnemar(table, exact=True).pvalue))


def _paired_bootstrap(baseline: list[bool], candidate: list[bool]) -> tuple[float, float]:
    """BCa paired bootstrap of mean(candidate - baseline) per family."""
    diff = np.asarray(candidate, dtype=float) - np.asarray(baseline, dtype=float)
    if diff.size < 2 or np.all(diff == diff[0]):
        value = float(diff.mean())
        return value, value
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            result = bootstrap(
                (np.asarray(baseline, dtype=float), np.asarray(candidate, dtype=float)),
                lambda base, cand, axis=-1: np.mean(cand - base, axis=axis),
                paired=True,
                vectorized=True,
                n_resamples=BOOTSTRAP_RESAMPLES,
                confidence_level=CONFIDENCE_LEVEL,
                method="BCa",
                rng=np.random.default_rng(BOOTSTRAP_SEED),
            )
        except (Warning, ValueError, FloatingPointError) as exc:
            raise SkillEvalError("non_finite") from exc
    low = float(result.confidence_interval.low)
    high = float(result.confidence_interval.high)
    if not (math.isfinite(low) and math.isfinite(high)):
        raise SkillEvalError("non_finite")
    return max(-1.0, low), min(1.0, high)


def _cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SkillEvalError("non_finite")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SkillEvalError("non_finite")
    return number
