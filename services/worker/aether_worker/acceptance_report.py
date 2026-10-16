"""Build the delivery report from a JUnit file and STATUS.json.

``passed`` is true only for a consistent tree with at least one executed case
and no failures, errors, or missing required cases. ``live_ready`` additionally
requires a binding that matches this JUnit, the referenced acceptance result,
and the current commit, environment, mode, config, and artifact hashes.
A stale ``live_ready: passed`` in STATUS cannot fill in missing or failed evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as parse_xml

from aether_worker.snapshot import SnapshotRejected, read_regular

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _count_attr(element: Any, name: str, *, required: bool) -> tuple[int, bool]:
    raw = element.attrib.get(name)
    if raw is None:
        return 0, not required
    if raw.isdigit() and len(raw) <= 9:
        return int(raw), True
    return 0, False


def _kind(case: Any) -> str:
    tags = {_local(child.tag) for child in list(case)}
    if "error" in tags:
        return "error"
    if "failure" in tags:
        return "failure"
    if "skipped" in tags:
        return "skipped"
    return "passed"


def _case_id(case: Any) -> str:
    classname = case.attrib.get("classname") or ""
    name = case.attrib.get("name") or ""
    if classname:
        return f"{classname}::{name}"
    return name


def _properties(root: Any) -> tuple[dict[str, str], bool]:
    found: dict[str, str] = {}
    for element in root.iter():
        if _local(element.tag) != "property":
            continue
        name = element.attrib.get("name")
        value = element.attrib.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return found, False
        if name in found and found[name] != value:
            return found, False
        found[name] = value
    return found, True


def _direct_cases(root: Any) -> tuple[list[Any], bool]:
    suites = [element for element in root.iter() if _local(element.tag) == "testsuite"]
    if not suites:
        return [], False
    direct = [child for suite in suites for child in list(suite) if _local(child.tag) == "testcase"]
    every = [element for element in root.iter() if _local(element.tag) == "testcase"]
    return direct, len(direct) == len(every)


def _suite_counts_match(root: Any, cases: list[Any]) -> bool:
    suites = [element for element in root.iter() if _local(element.tag) == "testsuite"]
    for suite in suites:
        own = [child for child in list(suite) if _local(child.tag) == "testcase"]
        kinds = [_kind(case) for case in own]
        declared_tests, tests_ok = _count_attr(suite, "tests", required=True)
        if not tests_ok or declared_tests != len(own):
            return False
        for name, kind in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
            declared, ok = _count_attr(suite, name, required=False)
            if not ok or declared != kinds.count(kind):
                return False
    if _local(root.tag) != "testsuites":
        return True
    kinds = [_kind(case) for case in cases]
    totals = {
        "tests": len(cases),
        "failures": kinds.count("failure"),
        "errors": kinds.count("error"),
        "skipped": kinds.count("skipped"),
    }
    for name, actual in totals.items():
        if name not in root.attrib:
            continue
        declared, ok = _count_attr(root, name, required=True)
        if not ok or declared != actual:
            return False
    return True


def _required(properties: Mapping[str, str], required_cases: Sequence[str] | None) -> list[str]:
    if required_cases is not None:
        return [item.strip() for item in required_cases if item.strip()]
    raw = properties.get("aether.required_cases", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _verdict(consistent: bool, counts: Mapping[str, int], missing: Sequence[str]) -> str:
    if not consistent:
        return "blocked"
    if counts["tests"] == 0 or counts["executed"] == 0:
        return "not_run"
    if counts["failures"] or counts["errors"] or missing:
        return "fail"
    return "pass"


def _parse_tree(root: Any, required_cases: Sequence[str] | None) -> dict[str, Any]:
    properties, properties_ok = _properties(root)
    cases, cases_ok = _direct_cases(root)
    recognized = _local(root.tag) in {"testsuite", "testsuites"}
    consistent = properties_ok and cases_ok and recognized and _suite_counts_match(root, cases)
    kinds = [_kind(case) for case in cases]
    counts = {
        "tests": len(cases),
        "failures": kinds.count("failure"),
        "errors": kinds.count("error"),
        "skipped": kinds.count("skipped"),
    }
    counts["executed"] = counts["tests"] - counts["skipped"]
    required = _required(properties, required_cases)
    passed_ids = [_case_id(case) for case, kind in zip(cases, kinds, strict=True) if kind == "passed"]
    missing = [item for item in required if item not in passed_ids]
    return {
        "counts": counts,
        "properties": properties,
        "consistent": consistent,
        "missing_required": missing,
        "status": _verdict(consistent, counts, missing),
    }


def _counts(root: Any, required_cases: Sequence[str] | None) -> dict[str, Any]:
    """Aggregate the whole JUnit tree. The name preserves the recorded call edge."""
    return _parse_tree(root, required_cases)


def _empty_parse() -> dict[str, Any]:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "executed": 0}
    return {"counts": counts, "properties": {}, "consistent": False, "missing_required": [], "status": "blocked"}


def _load_status(status_path: Path) -> dict[str, Any]:
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(status, dict):
        raise ValueError("status_not_object")
    return status


def _load_result(status: Mapping[str, Any], status_path: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    f26 = status.get("f26")
    if not isinstance(f26, dict):
        return None, None
    relative = f26.get("acceptance_result")
    if not isinstance(relative, str) or not relative:
        return None, None
    path = Path(relative)
    if path.is_absolute():
        return None, None
    try:
        data = read_regular(status_path.parent, path.parts)
    except SnapshotRejected:
        return None, None
    try:
        body = json.loads(data)
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    return body, data


def _text(binding: Mapping[str, Any], key: str) -> str | None:
    value = binding.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _artifacts(binding: Mapping[str, Any]) -> dict[str, str] | None:
    raw = binding.get("artifact_hashes")
    if not isinstance(raw, dict) or not raw:
        return None
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str) or _HEX64.fullmatch(value) is None:
            return None
        cleaned[key] = value
    return cleaned


def _identity(binding: Mapping[str, Any]) -> tuple[str, str, str, str, dict[str, str]] | None:
    commit = _text(binding, "git_commit")
    environment = _text(binding, "environment")
    mode = _text(binding, "mode")
    config = _text(binding, "config_sha256")
    artifacts = _artifacts(binding)
    if commit is None or environment is None or mode is None or config is None or artifacts is None:
        return None
    if _COMMIT.fullmatch(commit) is None or _HEX64.fullmatch(config) is None:
        return None
    if len(environment) > 200 or len(mode) > 200:
        return None
    return commit, environment, mode, config, artifacts


def _properties_match(properties: Mapping[str, str], identity: tuple[str, str, str, str, dict[str, str]], junit: bytes) -> bool:
    commit, environment, mode, config, artifacts = identity
    if artifacts.get("junit") != hashlib.sha256(junit).hexdigest():
        return False
    expected = {
        "aether.git_commit": commit,
        "aether.environment": environment,
        "aether.mode": mode,
        "aether.config_sha256": config,
    }
    if any(properties.get(name) != value for name, value in expected.items()):
        return False
    return all(properties.get(f"aether.artifact.{key}") == digest for key, digest in artifacts.items() if key != "junit")


def _result_matches(status: Mapping[str, Any], result_body: Mapping[str, Any], result_bytes: bytes, identity: tuple[str, str, str, str, dict[str, str]]) -> bool:
    commit = identity[0]
    acceptance = identity[4].get("acceptance")
    if acceptance is None or hashlib.sha256(result_bytes).hexdigest() != acceptance:
        return False
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    f26 = status.get("f26") if isinstance(status.get("f26"), dict) else {}
    head = f26.get("acceptance_head")
    if readiness.get("live_ready") != "passed" or f26.get("live_ready") is not True:
        return False
    if not isinstance(head, str) or not commit.startswith(head):
        return False
    return result_body.get("git_commit") == commit and result_body.get("live_ready") is True


def _binding_consistent(
    binding: Mapping[str, Any] | None,
    *,
    properties: Mapping[str, str],
    junit: bytes,
    status: Mapping[str, Any],
    result_body: Mapping[str, Any] | None,
    result_bytes: bytes | None,
) -> bool:
    if binding is None or result_body is None or result_bytes is None:
        return False
    identity = _identity(binding)
    if identity is None or not _properties_match(properties, identity, junit):
        return False
    return _result_matches(status, result_body, result_bytes, identity)


def _status_fields(status: Mapping[str, Any]) -> tuple[list[str], list[str], bool, str]:
    incomplete = [key for key, value in status.items() if isinstance(value, dict) and value.get("status") != "done"]
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    f26 = status.get("f26") if isinstance(status.get("f26"), dict) else {}
    not_run: list[str] = []
    if f26.get("status") != "done":
        not_run.append("F26 live model")
    if readiness.get("comparative_evidence") != "passed":
        not_run.append("Hermes comparison")
    if not bool(status.get("f15", {}).get("product_enabled")):
        not_run.append("CodeGraph product enablement")
    comparative = readiness.get("comparative_evidence", "not_run")
    if not isinstance(comparative, str):
        comparative = "not_run"
    return incomplete, not_run, bool(status.get("f15", {}).get("product_enabled")), comparative


def build_report(
    junit_path: Path,
    status_path: Path,
    *,
    binding: Mapping[str, Any] | None = None,
    required_cases: Sequence[str] | None = None,
) -> dict[str, Any]:
    status = _load_status(status_path)
    junit = junit_path.read_bytes()
    try:
        root = parse_xml(junit)
    except ParseError:
        parsed = _empty_parse()
    else:
        parsed = _counts(root, required_cases)
    result_body, result_bytes = (None, None) if binding is None else _load_result(status, status_path)
    consistent_binding = _binding_consistent(
        binding,
        properties=parsed["properties"],
        junit=junit,
        status=status,
        result_body=result_body,
        result_bytes=result_bytes,
    )
    incomplete, not_run, codegraph, comparative = _status_fields(status)
    artifacts = _artifacts(binding) if isinstance(binding, Mapping) else None
    verdict = parsed["status"]
    return {
        "source_junit": str(junit_path),
        "counts": parsed["counts"],
        "passed": verdict == "pass",
        "status": verdict,
        "executed": parsed["counts"]["executed"],
        "missing_required": list(parsed["missing_required"]),
        "kernel_engineering": "see_status",
        "product_engineering": "incomplete" if incomplete else "status_done",
        "live_ready": verdict == "pass" and consistent_binding,
        "binding_consistent": consistent_binding,
        "git_commit": _text(binding, "git_commit") if isinstance(binding, Mapping) else None,
        "environment": _text(binding, "environment") if isinstance(binding, Mapping) else None,
        "mode": _text(binding, "mode") if isinstance(binding, Mapping) else None,
        "config_sha256": _text(binding, "config_sha256") if isinstance(binding, Mapping) else None,
        "artifact_hashes": artifacts,
        "codegraph_enabled": codegraph,
        "comparative_evidence": comparative,
        "incomplete": incomplete,
        "not_run": not_run,
    }

