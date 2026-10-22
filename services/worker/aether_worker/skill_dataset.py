"""Dataset manifest and candidate bundle read before any grader starts."""

from __future__ import annotations

from typing import Any

from aether_worker.bundle_files import BundleError, load_object, parse_file_map

_DATASET = "aether.skill_dataset"
_CANDIDATE = "aether.skill_candidate"
_SPLITS = {"development", "validation", "release", "holdout"}


class DatasetError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_dataset(data: bytes) -> dict[str, Any]:
    try:
        body = load_object(data)
    except BundleError as exc:
        raise DatasetError("dataset_invalid") from exc
    if body.get("kind") != _DATASET or body.get("split") not in _SPLITS:
        raise DatasetError("dataset_invalid")
    if not isinstance(body.get("synthetic"), bool) or not isinstance(body.get("contract"), str) or not body["contract"].strip():
        raise DatasetError("dataset_invalid")
    cases = body.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 64:
        raise DatasetError("dataset_invalid")
    parsed_cases = [_case(item) for item in cases]
    families = [item["family"] for item in parsed_cases]
    if len(families) != len(set(families)):
        raise DatasetError("dataset_invalid")
    baseline_outcomes = _baseline_outcomes(parsed_cases, body)
    if baseline_outcomes is None:
        baseline_passes = _count(body.get("baseline_passes", 0), len(parsed_cases))
        # A bare count pairs only when every family failed or every family passed.
        # Anything in between is baseline_unpaired; reject it here so grading never starts.
        if baseline_passes not in (0, len(parsed_cases)):
            raise DatasetError("baseline_unpaired")
    else:
        baseline_passes = sum(baseline_outcomes)
    return {
        "split": str(body["split"]),
        "synthetic": body["synthetic"],
        "contract": body["contract"],
        "baseline_passes": baseline_passes,
        "baseline_outcomes": baseline_outcomes,
        "baseline_cost": _cost(body.get("baseline_cost", 0)),
        "candidate_cost": _cost(body.get("candidate_cost", 0)),
        "cases": parsed_cases,
    }


def parse_candidate(data: bytes) -> dict[str, Any]:
    try:
        body = load_object(data)
        if body.get("kind") != _CANDIDATE:
            raise BundleError("bundle_invalid")
        files = parse_file_map(body.get("files"))
    except BundleError as exc:
        raise exc
    return {"files": files}


def _case(item: object) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) - {"family", "suite_files", "baseline_pass"}:
        raise DatasetError("dataset_invalid")
    if "baseline_pass" in item and not isinstance(item["baseline_pass"], bool):
        raise DatasetError("dataset_invalid")
    family = item.get("family")
    if not isinstance(family, str) or not family.strip() or len(family) > 128:
        raise DatasetError("dataset_invalid")
    try:
        files = parse_file_map(item.get("suite_files"))
    except BundleError as exc:
        raise DatasetError(exc.code if exc.code == "path_rejected" else "dataset_invalid") from exc
    parsed: dict[str, Any] = {"family": family, "suite_files": files}
    if "baseline_pass" in item:
        parsed["baseline_pass"] = item["baseline_pass"]
    return parsed


def _baseline_outcomes(cases: list[dict[str, Any]], body: dict[str, Any]) -> list[bool] | None:
    """Per-family baseline results for a paired comparison; all cases or none."""
    marked = [case for case in cases if "baseline_pass" in case]
    if not marked:
        return None
    if len(marked) != len(cases):
        raise DatasetError("dataset_invalid")
    outcomes = [bool(case["baseline_pass"]) for case in cases]
    if "baseline_passes" in body and _count(body["baseline_passes"], len(cases)) != sum(outcomes):
        raise DatasetError("dataset_invalid")
    return outcomes


def _count(value: object, families: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > families:
        raise DatasetError("dataset_invalid")
    return value


def _cost(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetError("dataset_invalid")
    return value
