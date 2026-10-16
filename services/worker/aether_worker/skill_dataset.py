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
    return {
        "split": str(body["split"]),
        "synthetic": body["synthetic"],
        "contract": body["contract"],
        "baseline_passes": _count(body.get("baseline_passes", 0), len(parsed_cases)),
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
    if not isinstance(item, dict) or set(item) - {"family", "suite_files"}:
        raise DatasetError("dataset_invalid")
    family = item.get("family")
    if not isinstance(family, str) or not family.strip() or len(family) > 128:
        raise DatasetError("dataset_invalid")
    try:
        files = parse_file_map(item.get("suite_files"))
    except BundleError as exc:
        raise DatasetError(exc.code if exc.code == "path_rejected" else "dataset_invalid") from exc
    return {"family": family, "suite_files": files}


def _count(value: object, families: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > families:
        raise DatasetError("dataset_invalid")
    return value


def _cost(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetError("dataset_invalid")
    return value
