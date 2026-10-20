"""Uplift is a paired before/after comparison on the same task families."""

from __future__ import annotations

import json
import math

import pytest
from aether_worker.skill_dataset import DatasetError, parse_dataset
from aether_worker.skill_eval import SkillEvalError
from aether_worker.skill_metrics import metrics_from_grades


def _grades(outcomes: list[bool]) -> list[dict[str, object]]:
    return [{"verdict": "PASS" if ok else "FAIL", "sentinel_visible": False, "credential_present": False} for ok in outcomes]


def _exact_mcnemar(baseline_only: int, candidate_only: int) -> float:
    """Two-sided exact binomial test on the discordant pairs, p = 0.5."""
    n = baseline_only + candidate_only
    if n == 0:
        return 1.0
    k = min(baseline_only, candidate_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def _pairs(both: int, baseline_only: int, candidate_only: int, neither: int) -> tuple[list[bool], list[bool]]:
    baseline = [True] * both + [True] * baseline_only + [False] * candidate_only + [False] * neither
    candidate = [True] * both + [False] * baseline_only + [True] * candidate_only + [False] * neither
    return baseline, candidate


def test_uplift_uses_paired_mcnemar_and_bootstrap_ci() -> None:
    # Same marginals (baseline 10/40, candidate 18/40), different pairing.
    nested_base, nested_cand = _pairs(10, 0, 8, 22)
    mixed_base, mixed_cand = _pairs(5, 5, 13, 17)
    assert sum(nested_base) == sum(mixed_base) == 10
    assert sum(nested_cand) == sum(mixed_cand) == 18

    names = [f"family-{i}" for i in range(40)]
    nested = metrics_from_grades(
        _grades(nested_cand), baseline_outcomes=nested_base, family_names=names, candidate_cost=1, baseline_cost=1
    )
    mixed = metrics_from_grades(
        _grades(mixed_cand), baseline_outcomes=mixed_base, family_names=names, candidate_cost=1, baseline_cost=1
    )

    for metrics, (b, c) in ((nested, (0, 8)), (mixed, (5, 13))):
        assert metrics["families"] == 40
        assert metrics["grade_passes"] == 18
        assert metrics["success_delta"] == pytest.approx(8 / 40)
        assert metrics["significance_test"] == "mcnemar_exact"
        assert metrics["discordant"] == {"baseline_only": b, "candidate_only": c}
        assert metrics["p_value"] == pytest.approx(_exact_mcnemar(b, c), rel=1e-9, abs=1e-12)
        assert metrics["ci_method"] == "paired_bootstrap"
        assert isinstance(metrics["bootstrap_seed"], int)
        assert metrics["confidence_level"] == 0.95
        assert -1.0 <= metrics["ci_lower"] <= metrics["success_delta"] <= metrics["ci_upper"] <= 1.0
        records = metrics["paired"]
        assert [row["family"] for row in records] == names
        assert [row["baseline"] for row in records] == (nested_base if metrics is nested else mixed_base)
        assert [row["candidate"] for row in records] == (nested_cand if metrics is nested else mixed_cand)
        json.dumps(metrics)

    # Pairing changes the evidence even though both marginals are identical.
    assert nested["p_value"] < mixed["p_value"]
    assert nested["ci_lower"] > mixed["ci_lower"]
    # The old "Wilson lower bound of the candidate minus the baseline point rate" is not used.
    phat, n, z = 18 / 40, 40, 1.959963984540054
    centre = (phat + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n) / (1 + z * z / n)
    assert nested["ci_lower"] != pytest.approx(centre - margin - 10 / 40)

    # Fixed seed: the interval is reproducible.
    again = metrics_from_grades(
        _grades(nested_cand), baseline_outcomes=nested_base, family_names=names, candidate_cost=1, baseline_cost=1
    )
    assert (again["ci_lower"], again["ci_upper"]) == (nested["ci_lower"], nested["ci_upper"])


def test_uplift_counts_a_security_failure_as_a_failed_candidate_pair() -> None:
    grades = _grades([True, True])
    grades[1]["sentinel_visible"] = True
    metrics = metrics_from_grades(grades, baseline_outcomes=[False, True], candidate_cost=0, baseline_cost=0)
    assert metrics["security_failure"] is True
    assert [row["candidate"] for row in metrics["paired"]] == [True, False]
    assert metrics["discordant"] == {"baseline_only": 1, "candidate_only": 1}


def test_unpaired_baseline_is_rejected() -> None:
    with pytest.raises(SkillEvalError) as short:
        metrics_from_grades(_grades([True, False]), baseline_outcomes=[True], candidate_cost=0, baseline_cost=0)
    assert short.value.code == "non_finite"
    with pytest.raises(SkillEvalError) as names:
        metrics_from_grades(
            _grades([True, False]), baseline_outcomes=[True, False], family_names=["a"], candidate_cost=0, baseline_cost=0
        )
    assert names.value.code == "non_finite"
    with pytest.raises(SkillEvalError) as count_only:
        metrics_from_grades(_grades([True, False, True]), baseline_passes=1, candidate_cost=0, baseline_cost=0)
    assert count_only.value.code == "baseline_unpaired"


def _dataset(cases: list[dict[str, object]], **extra: object) -> bytes:
    body = {"kind": "aether.skill_dataset", "split": "development", "synthetic": False, "contract": "c", "cases": cases, **extra}
    return json.dumps(body).encode()


def test_dataset_carries_a_per_family_baseline() -> None:
    files = {"test_x.py": "def test_x():\n    assert True\n"}
    cases = [{"family": "a", "suite_files": files, "baseline_pass": True}, {"family": "b", "suite_files": files, "baseline_pass": False}]
    parsed = parse_dataset(_dataset(cases))
    assert parsed["baseline_outcomes"] == [True, False]
    assert parsed["baseline_passes"] == 1
    with pytest.raises(DatasetError):
        parse_dataset(_dataset(cases, baseline_passes=2))
    with pytest.raises(DatasetError):
        parse_dataset(_dataset([{"family": "a", "suite_files": files, "baseline_pass": "yes"}]))
    partial = [{"family": "a", "suite_files": files, "baseline_pass": True}, {"family": "b", "suite_files": files}]
    with pytest.raises(DatasetError):
        parse_dataset(_dataset(partial))


@pytest.mark.parametrize(
    "category",
    [pytest.param("degenerate", id="scipy-degenerate-data"), pytest.param("runtime", id="numpy-runtime-warning")],
)
def test_a_bootstrap_warning_becomes_non_finite(monkeypatch: pytest.MonkeyPatch, category: str) -> None:
    """A warning the bootstrap raises (warnings are errors there) is a SkillEvalError, not a crash.

    Fault injection: the real SciPy bootstrap still runs; it is wrapped so that it
    first emits the warning SciPy/NumPy emit on degenerate resamples.
    """
    import warnings

    import scipy.stats
    from scipy.stats import DegenerateDataWarning

    real = scipy.stats.bootstrap

    def noisy(*args: object, **kwargs: object) -> object:
        if category == "degenerate":
            warnings.warn("The BCa confidence interval cannot be calculated.", DegenerateDataWarning, stacklevel=2)
        else:
            warnings.warn("invalid value encountered in divide", RuntimeWarning, stacklevel=2)
        return real(*args, **kwargs)

    monkeypatch.setattr(scipy.stats, "bootstrap", noisy)
    base, cand = _pairs(5, 5, 13, 17)
    with pytest.raises(SkillEvalError) as rejected:
        metrics_from_grades(_grades(cand), baseline_outcomes=base, candidate_cost=1, baseline_cost=1)
    assert rejected.value.code == "non_finite"
