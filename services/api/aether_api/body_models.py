"""Request bodies. Field rules live in Pydantic validators and raise the existing ApiError codes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationInfo, model_validator

from aether_api.errors import ApiError
from aether_domain.security import sha256_text
from aether_worker.memory import ACTIONS, CATEGORIES, KINDS

_SOURCE_KINDS = {"url", "rss", "local_document", "github_api", "arxiv_api"}
_FEEDBACK_KINDS = {"click", "save", "helpful", "dwell", "hide", "unsave"}
_SCORE_KEYS = {"metrics", "score", "decision", "success_delta", "promoted"}
_COMMANDS = {"approve_goal", "pause", "resume", "cancel"}
_TEMPLATES = {
    "artifact_exists",
    "json_schema",
    "trusted_test_suite",
    "citation_integrity",
    "source_coverage",
    "human_review",
}
_BUDGET_KEYS = {"max_cost_micro_usd", "max_tokens", "max_model_requests", "active_seconds", "verification_reserve_fraction"}


def _reject(code: str, message: str) -> None:
    raise ApiError(422, code, message)


def _identifier(value: object, *, status: int, code: str, message: str) -> str:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ApiError(status, code, message) from exc
    return str(value)


def _not_found_id(value: object) -> str:
    return _identifier(value, status=404, code="not_found", message="任务不存在")


class BootstrapBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    bootstrap_secret: Any
    display_name: Any
    passphrase: Any

    @model_validator(mode="before")
    @classmethod
    def present(cls, data: object) -> object:
        if not isinstance(data, dict) or any(name not in data for name in ("bootstrap_secret", "display_name", "passphrase")):
            _reject("invalid_body", "请求体缺少字段")
        return data


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    passphrase: Any

    @model_validator(mode="before")
    @classmethod
    def present(cls, data: object) -> object:
        if not isinstance(data, dict) or "passphrase" not in data:
            _reject("invalid_body", "请求体缺少字段")
        return data


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, str]:
        if not isinstance(data, dict) or set(data) - {"name"}:
            _reject("invalid_project", "项目名称不合法")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            _reject("invalid_project", "项目名称不合法")
        return {"name": name}


class BudgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_cost_micro_usd: int
    max_tokens: int
    max_model_requests: int
    active_seconds: int
    verification_reserve_fraction: float

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - _BUDGET_KEYS:
            _reject("invalid_budget", "任务预算不合法")
        spec: dict[str, Any] = {
            "max_cost_micro_usd": 2_000_000,
            "max_tokens": 100_000,
            "max_model_requests": 48,
            "active_seconds": 1800,
            "verification_reserve_fraction": 0.2,
        }
        spec.update(data)
        if any(isinstance(spec[name], bool) or not isinstance(spec[name], int) for name in ("max_cost_micro_usd", "max_tokens", "max_model_requests", "active_seconds")):
            _reject("invalid_budget", "任务预算不合法")
        fraction = spec["verification_reserve_fraction"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            _reject("invalid_budget", "任务预算不合法")
        spec["verification_reserve_fraction"] = float(fraction)
        if spec["max_cost_micro_usd"] < 0 or spec["max_tokens"] < 1 or spec["max_model_requests"] < 1 or spec["active_seconds"] < 1:
            _reject("invalid_budget", "任务预算不合法")
        if not 0 <= spec["verification_reserve_fraction"] < 1:
            _reject("invalid_budget", "任务预算不合法")
        return spec


class SourceIdList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: list[str]

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, list[str]]:
        if not isinstance(data, list):
            _reject("invalid_sources", "允许的来源不合法")
        parsed: list[str] = []
        for item in data:
            if not isinstance(item, str):
                _reject("invalid_sources", "允许的来源不合法")
            try:
                parsed.append(str(uuid.UUID(item)))
            except ValueError as exc:
                raise ApiError(422, "invalid_sources", "允许的来源不合法") from exc
        if len(parsed) != len(set(parsed)):
            _reject("invalid_sources", "允许的来源不合法")
        return {"values": parsed}


class CreateTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    budget: dict[str, Any] | None = None
    allowed_source_ids: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - {"goal", "budget", "allowed_source_ids"}:
            _reject("invalid_task", "任务目标不合法")
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 16000:
            _reject("invalid_task", "任务目标不合法")
        budget = BudgetSpec.model_validate(data["budget"]).model_dump() if "budget" in data else None
        sources = SourceIdList.model_validate(data["allowed_source_ids"]).values if "allowed_source_ids" in data else None
        return {"goal": goal, "budget": budget, "allowed_source_ids": sources}


def _criterion(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("criterion_id"), str) and item.get("template") in _TEMPLATES and isinstance(item.get("description"), str) and bool(item["description"])


class ContractBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object, info: ValidationInfo) -> object:
        if not isinstance(data, dict):
            _reject("invalid_contract", "合同不合法")
        context = info.context or {}
        if data.get("project_id") != context.get("project_id") or data.get("task_id") != context.get("task_id"):
            _reject("invalid_contract", "合同不属于这个任务")
        goal = data.get("goal")
        artifacts = data.get("required_artifacts")
        criteria = data.get("criteria")
        created_at = data.get("created_at")
        if not isinstance(goal, str) or not goal.strip():
            _reject("invalid_contract", "合同缺少目标")
        if not isinstance(artifacts, list) or not artifacts or not all(isinstance(item, str) and item for item in artifacts):
            _reject("invalid_contract", "合同缺少产物")
        if not isinstance(criteria, list) or not criteria or not all(_criterion(item) for item in criteria):
            _reject("invalid_contract", "合同缺少验收条款")
        if not isinstance(created_at, str) or not created_at:
            _reject("invalid_contract", "合同缺少时间")
        version = data.get("contract_version")
        if not isinstance(version, int) or version < 1:
            _reject("invalid_contract", "合同版本不合法")
        return data


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str
    kind: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, str]:
        if not isinstance(data, dict):
            _reject("invalid_command", "命令不合法")
        command_id = data.get("command_id")
        kind = data.get("kind")
        try:
            uuid.UUID(str(command_id))
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_command", "命令编号不合法") from exc
        if kind not in _COMMANDS or set(data) - {"command_id", "kind", "expected_contract_sha256", "reason"}:
            _reject("invalid_command", "命令不合法")
        return {"command_id": str(command_id), "kind": str(kind)}


class ApproveExtraBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str
    expected_contract_sha256: str | None = None

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict):
            _reject("invalid_command", "命令不合法")
        body = data.get("body")
        owner_id = data.get("owner_id")
        if not isinstance(body, dict) or not isinstance(owner_id, str):
            _reject("invalid_command", "命令不合法")
        expected = body.get("expected_contract_sha256")
        if expected is None:
            return {"owner_id": owner_id, "expected_contract_sha256": None}
        if not isinstance(expected, str) or len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
            _reject("invalid_command", "命令不合法")
        return {"owner_id": owner_id, "expected_contract_sha256": expected}


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    expected_intent_hash: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, str]:
        if not isinstance(data, dict):
            _reject("invalid_decision", "审批决定不合法")
        decision = data.get("decision")
        digest = data.get("expected_intent_hash")
        if decision not in {"approve", "reject"} or not isinstance(digest, str) or len(digest) != 64:
            _reject("invalid_decision", "审批决定不合法")
        if set(data) - {"decision", "expected_intent_hash", "reason"}:
            _reject("invalid_decision", "审批决定不合法")
        return {"decision": str(decision), "expected_intent_hash": digest}


class SourceCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    locator: str
    allowed_domains: list[str]
    fetch_interval_seconds: int

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - {"kind", "locator", "allowed_domains", "fetch_interval_seconds"}:
            _reject("invalid_source", "来源不合法")
        kind = data.get("kind")
        locator = data.get("locator")
        domains = data.get("allowed_domains") or []
        interval = data.get("fetch_interval_seconds", 3600)
        if kind not in _SOURCE_KINDS or not isinstance(locator, str) or not locator.strip() or len(locator) > 4000:
            _reject("invalid_source", "来源不合法")
        if not isinstance(domains, list) or any(not isinstance(item, str) or not item for item in domains):
            _reject("invalid_source", "域名不合法")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 60:
            _reject("invalid_source", "抓取间隔不合法")
        return {"kind": kind, "locator": locator, "allowed_domains": domains, "fetch_interval_seconds": interval}


class SourcePatchBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    allowed_domains: list[str] | None = None
    fetch_interval_seconds: int | None = None
    status: str | None = None

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or not data or set(data) - {"allowed_domains", "fetch_interval_seconds", "status"}:
            _reject("invalid_source", "来源更新不合法")
        parsed = SourceCreateBody.model_validate(
            {
                "kind": "url",
                "locator": "placeholder",
                "allowed_domains": data.get("allowed_domains", []),
                "fetch_interval_seconds": data.get("fetch_interval_seconds", 3600),
            }
        )
        result: dict[str, Any] = {}
        if "allowed_domains" in data:
            result["allowed_domains"] = parsed.allowed_domains
        if "fetch_interval_seconds" in data:
            result["fetch_interval_seconds"] = parsed.fetch_interval_seconds
        if "status" in data:
            if data["status"] not in {"ACTIVE", "PAUSED"}:
                _reject("invalid_source", "来源状态不合法")
            result["status"] = data["status"]
        return result


class IngestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, str]:
        if not isinstance(data, dict) or set(data) != {"source_id"}:
            _reject("invalid_ingestion", "采集请求不合法")
        return {"source_id": _not_found_id(data["source_id"])}


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    mode: str
    limit: int
    source_ids: list[str]

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object, info: ValidationInfo) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - {"project_id", "query", "mode", "limit", "source_ids"}:
            _reject("invalid_search", "检索请求不合法")
        query = data.get("query")
        mode = data.get("mode", "hybrid")
        limit = data.get("limit", 8)
        source_ids = data.get("source_ids") or []
        project_id = (info.context or {}).get("project_id")
        if data.get("project_id") != project_id or not isinstance(query, str) or not query.strip() or len(query) > 4000:
            _reject("invalid_search", "检索请求不合法")
        if mode not in {"lexical", "dense", "hybrid"}:
            _reject("invalid_search", "检索模式不合法")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 50:
            _reject("invalid_search", "检索条数不合法")
        if not isinstance(source_ids, list):
            _reject("invalid_search", "来源过滤不合法")
        parsed = [_not_found_id(item) for item in source_ids]
        return {"query": query.strip(), "mode": mode, "limit": limit, "source_ids": parsed}


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    impression_id: str | None
    source_id: str
    kind: str
    dwell_seconds: int | float | None
    event_time: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or "user" in data or set(data) - {"event_id", "impression_id", "source_id", "kind", "dwell_seconds", "event_time"}:
            _reject("invalid_feedback", "反馈不合法")
        event_id = _not_found_id(data.get("event_id"))
        source_id = _not_found_id(data.get("source_id"))
        impression = data.get("impression_id")
        if impression is not None:
            impression = _not_found_id(impression)
        if data.get("kind") not in _FEEDBACK_KINDS or not isinstance(data.get("event_time"), str):
            _reject("invalid_feedback", "反馈不合法")
        try:
            datetime.fromisoformat(str(data["event_time"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError(422, "invalid_range", "时间范围不合法") from exc
        dwell = data.get("dwell_seconds")
        if dwell is not None and (isinstance(dwell, bool) or not isinstance(dwell, (int, float)) or dwell < 0):
            _reject("invalid_feedback", "停留时间不合法")
        return {
            "event_id": event_id,
            "impression_id": impression,
            "source_id": source_id,
            "kind": data["kind"],
            "dwell_seconds": dwell,
            "event_time": data["event_time"],
        }


class SkillImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_artifact_id: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, str]:
        if not isinstance(data, dict) or set(data) != {"bundle_artifact_id"}:
            _reject("invalid_skill", "技能导入不合法")
        return {"bundle_artifact_id": _not_found_id(data["bundle_artifact_id"])}


class SkillEvaluateBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    candidate_version_id: str
    baseline_version_id: str | None
    dataset_manifest_id: str
    budget: dict[str, Any]
    configuration: dict[str, str]

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or _SCORE_KEYS & set(data) or set(data) - {"candidate_version_id", "baseline_version_id", "dataset_manifest_id", "budget"}:
            _reject("invalid_evaluation", "评测请求不能自带分数")
        candidate = _not_found_id(data.get("candidate_version_id"))
        dataset = _not_found_id(data.get("dataset_manifest_id"))
        baseline = data.get("baseline_version_id")
        if baseline is not None:
            baseline = _not_found_id(baseline)
        if not isinstance(data.get("budget"), dict):
            _reject("invalid_evaluation", "评测预算不合法")
        return {
            "candidate_version_id": candidate,
            "baseline_version_id": baseline,
            "dataset_manifest_id": dataset,
            "budget": data["budget"],
            "configuration": {"kind": "skill.evaluate"},
        }


class DeployBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    target_version_id: str
    evaluation_id: str
    expected_previous_version_id: Any
    approval_id: str
    report_sha256: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        required = {"action", "target_version_id", "evaluation_id", "expected_previous_version_id", "approval_id", "report_sha256"}
        if not isinstance(data, dict) or set(data) != required:
            _reject("invalid_deploy", "部署请求不合法")
        if data["action"] not in {"promote", "rollback"}:
            _reject("invalid_deploy", "部署动作不合法")
        for key in ("target_version_id", "evaluation_id", "approval_id"):
            _not_found_id(data[key])
        previous = data["expected_previous_version_id"]
        if previous is not None:
            _not_found_id(previous)
        digest = data["report_sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            _reject("invalid_deploy", "报告哈希不合法")
        return {
            "action": data["action"],
            "target_version_id": str(data["target_version_id"]),
            "evaluation_id": str(data["evaluation_id"]),
            "expected_previous_version_id": previous,
            "approval_id": str(data["approval_id"]),
            "report_sha256": digest,
        }


class CodingJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    base_artifact_id: str
    job_key: str
    objective: str

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, str]:
        if not isinstance(data, dict) or set(data) != {"task_id", "base_artifact_id", "job_key", "objective"}:
            _reject("invalid_coding_job", "编码作业不合法")
        _not_found_id(data["task_id"])
        _not_found_id(data["base_artifact_id"])
        job_key = data["job_key"]
        objective = data["objective"]
        if not isinstance(job_key, str) or not job_key.strip() or len(job_key) > 128:
            _reject("invalid_coding_job", "作业键不合法")
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
            _reject("invalid_coding_job", "目标不合法")
        return {
            "task_id": str(data["task_id"]),
            "base_artifact_id": str(data["base_artifact_id"]),
            "job_key": job_key.strip(),
            "objective": objective.strip(),
        }


class WatchCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str
    source_ids: list[str]
    schedule: dict[str, Any]
    budget: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) != {"purpose", "source_ids", "schedule", "budget"}:
            _reject("invalid_watch", "跟踪请求不合法")
        purpose = data["purpose"]
        sources = data["source_ids"]
        schedule = data["schedule"]
        if not isinstance(purpose, str) or not purpose.strip() or not isinstance(sources, list) or not sources:
            _reject("invalid_watch", "跟踪请求不合法")
        parsed_sources = [_not_found_id(item) for item in sources]
        if not isinstance(schedule, dict) or set(schedule) - {"interval_seconds", "timezone"} or not isinstance(schedule.get("interval_seconds"), int):
            _reject("invalid_watch", "跟踪计划不合法")
        if isinstance(schedule["interval_seconds"], bool) or schedule["interval_seconds"] < 300:
            _reject("invalid_watch", "跟踪间隔不合法")
        if not isinstance(data["budget"], dict):
            _reject("invalid_watch", "跟踪预算不合法")
        return {
            "purpose": purpose,
            "source_ids": parsed_sources,
            "schedule": {"interval_seconds": schedule["interval_seconds"], "timezone": schedule.get("timezone", "Australia/Melbourne")},
            "budget": data["budget"],
        }


class WatchPatchBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str | None = None
    interval_seconds: int | None = None
    expected_source_ids: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or not data or set(data) - {"status", "interval_seconds", "expected_source_ids"}:
            _reject("invalid_watch", "跟踪更新不合法")
        result: dict[str, Any] = {}
        if "status" in data:
            if data["status"] not in {"ACTIVE", "PAUSED"}:
                _reject("invalid_watch", "跟踪状态不合法")
            result["status"] = data["status"]
        if "interval_seconds" in data:
            if isinstance(data["interval_seconds"], bool) or not isinstance(data["interval_seconds"], int) or data["interval_seconds"] < 300:
                _reject("invalid_watch", "跟踪间隔不合法")
            result["interval_seconds"] = data["interval_seconds"]
        if "expected_source_ids" in data:
            if not isinstance(data["expected_source_ids"], list):
                _reject("invalid_watch", "来源列表不合法")
            result["expected_source_ids"] = [str(item) for item in data["expected_source_ids"]]
        return result


class EvaluationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str
    dataset_manifest_id: str
    configuration: dict[str, Any]
    budget: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or _SCORE_KEYS & set(data) or set(data) != {"purpose", "dataset_manifest_id", "configuration", "budget"}:
            _reject("invalid_evaluation", "评测请求不能自带分数")
        if data["purpose"] not in {"development", "validation", "release"} or not isinstance(data["configuration"], dict) or not isinstance(data["budget"], dict):
            _reject("invalid_evaluation", "评测请求不合法")
        return {
            "purpose": data["purpose"],
            "dataset_manifest_id": _not_found_id(data["dataset_manifest_id"]),
            "configuration": data["configuration"],
            "budget": data["budget"],
        }


class ConnectorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str
    project_id: str
    connector_key: str
    transport: str
    manifest: dict[str, Any]
    manifest_sha256: str
    secret_reference: str | None

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        allowed = {"project_id", "connector_key", "transport", "manifest", "secret_value"}
        if not isinstance(data, dict) or set(data) - allowed or set(data) & {"project_id", "connector_key", "transport", "manifest"} != {"project_id", "connector_key", "transport", "manifest"}:
            _reject("invalid_connector", "连接器不合法")
        project_id = _not_found_id(data["project_id"])
        if data["transport"] not in {"stdio", "streamable_http"} or not isinstance(data["connector_key"], str) or not data["connector_key"].strip():
            _reject("invalid_connector", "连接器不合法")
        if not isinstance(data["manifest"], dict):
            _reject("invalid_connector", "清单不合法")
        secret = data.get("secret_value")
        if secret is not None and not isinstance(secret, str):
            _reject("invalid_connector", "密钥不合法")
        manifest = {key: value for key, value in data["manifest"].items() if key != "secret_value"}
        raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return {
            "connector_id": str(uuid.uuid4()),
            "project_id": project_id,
            "connector_key": data["connector_key"].strip(),
            "transport": data["transport"],
            "manifest": manifest,
            "manifest_sha256": sha256_text(raw),
            "secret_reference": None if not secret else "sha256:" + sha256_text(secret),
        }


class ModelProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str
    allow_billable_probe: bool
    max_cost_micro_usd: int

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) != {"alias", "allow_billable_probe", "max_cost_micro_usd"}:
            _reject("invalid_probe", "模型探针不合法")
        if not isinstance(data["alias"], str) or not data["alias"].strip() or data["allow_billable_probe"] is not True:
            _reject("invalid_probe", "模型探针不合法")
        if isinstance(data["max_cost_micro_usd"], bool) or not isinstance(data["max_cost_micro_usd"], int) or data["max_cost_micro_usd"] < 1:
            _reject("invalid_probe", "费用上限不合法")
        return {
            "alias": data["alias"],
            "allow_billable_probe": True,
            "max_cost_micro_usd": data["max_cost_micro_usd"],
        }


class MemoryCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str
    kind: str
    category: str
    source_artifact_ids: list[str]

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - {"claim", "kind", "category", "source_artifact_ids"}:
            _reject("invalid_memory", "记忆请求不合法")
        claim = data.get("claim")
        kind = data.get("kind")
        category = data.get("category")
        sources = data.get("source_artifact_ids") or []
        if not isinstance(claim, str) or not claim.strip() or kind not in KINDS or category not in CATEGORIES or not isinstance(sources, list):
            _reject("invalid_memory", "记忆请求不合法")
        parsed: list[str] = []
        for item in sources:
            try:
                uuid.UUID(str(item))
            except (TypeError, ValueError) as exc:
                raise ApiError(422, "invalid_memory", "来源编号不合法") from exc
            parsed.append(str(item))
        return {"claim": claim.strip(), "kind": kind, "category": category, "source_artifact_ids": parsed}


class MemoryPatchBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str
    claim: str | None = None
    sensitivity: str | None = None
    authorization_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def check(cls, data: object) -> dict[str, Any]:
        if not isinstance(data, dict) or data.get("action") not in ACTIONS:
            _reject("invalid_memory", "记忆修正不合法")
        action = str(data["action"])
        if action == "declassify":
            if set(data) - {"action", "sensitivity", "authorization_id"}:
                _reject("invalid_memory", "记忆修正不合法")
            sensitivity = data.get("sensitivity")
            authorization_id = data.get("authorization_id")
            if sensitivity not in {"standard", "high"}:
                _reject("invalid_memory", "记忆修正不合法")
            try:
                uuid.UUID(str(authorization_id))
            except (TypeError, ValueError) as exc:
                raise ApiError(422, "invalid_memory", "记忆修正不合法") from exc
            return {"action": "declassify", "claim": None, "sensitivity": str(sensitivity), "authorization_id": str(authorization_id)}
        if set(data) - {"claim", "action"}:
            _reject("invalid_memory", "记忆修正不合法")
        claim = data.get("claim")
        if claim is not None and (not isinstance(claim, str) or not claim.strip()):
            _reject("invalid_memory", "记忆修正不合法")
        if action in {"correct", "supersede"} and not isinstance(claim, str):
            _reject("invalid_memory", "修正必须带新的说法")
        return {"action": action, "claim": None if claim is None else claim.strip(), "sensitivity": None, "authorization_id": None}
