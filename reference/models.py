"""AETHER-owned domain contracts. Upstream adapters must translate to these types.

These models validate shape and local invariants. They do not authenticate a
caller, enforce database isolation, or prove that evidence is trustworthy.
"""
from __future__ import annotations
from enum import StrEnum
from typing import Annotated, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]

class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

class TaskState(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class IdempotencyClass(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    RECONCILABLE_WRITE = "reconcilable_write"
    NON_IDEMPOTENT = "non_idempotent"

class OperationState(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHED = "DISPATCHED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

class RiskClass(StrEnum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"

class BudgetSpec(ContractModel):
    max_cost_micro_usd: NonNegativeInt = 2_000_000
    max_tokens: Annotated[int, Field(strict=True, ge=1)] = 100_000
    max_model_requests: Annotated[int, Field(strict=True, ge=1)] = 48
    active_seconds: Annotated[int, Field(strict=True, ge=1)] = 1800
    verification_reserve_fraction: Annotated[float, Field(ge=0, lt=1)] = 0.20

class AcceptanceCriterion(ContractModel):
    criterion_id: Identifier
    template: Literal["artifact_exists", "json_schema", "trusted_test_suite", "citation_integrity", "source_coverage", "human_review"]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    mandatory: bool = True
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    trusted_suite_hash: Digest | None = None

    @model_validator(mode="after")
    def require_suite_hash(self):
        if self.template == "trusted_test_suite" and self.trusted_suite_hash is None:
            raise ValueError("trusted_test_suite requires a frozen suite hash")
        return self

class GoalContract(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: Identifier
    task_id: Identifier
    contract_version: Annotated[int, Field(strict=True, ge=1)]
    goal: Annotated[str, Field(min_length=1, max_length=16000)]
    assumptions: list[str] = Field(default_factory=list)
    allowed_source_ids: list[Identifier] = Field(default_factory=list)
    required_artifacts: Annotated[list[str], Field(min_length=1)]
    criteria: Annotated[list[AcceptanceCriterion], Field(min_length=1)]
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_criteria(self):
        ids = [c.criterion_id for c in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion IDs must be unique")
        if not any(c.mandatory for c in self.criteria):
            raise ValueError("at least one mandatory criterion is required")
        return self

class TaskNode(ContractModel):
    node_id: Identifier
    role: Literal["research", "coding", "extraction", "verification"]
    objective: Annotated[str, Field(min_length=1)]
    dependencies: list[Identifier] = Field(default_factory=list)
    resource_ids: list[Identifier] = Field(default_factory=list)
    max_turns: Annotated[int, Field(strict=True, ge=1, le=64)] = 16

class Plan(ContractModel):
    nodes: Annotated[list[TaskNode], Field(min_length=1, max_length=32)]
    max_parallel: Annotated[int, Field(strict=True, ge=1, le=2)] = 2

class ToolCallProposal(ContractModel):
    tool_id: Identifier
    arguments: dict[str, JsonValue]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]

class Decision(ContractModel):
    kind: Literal["call_tools", "submit_candidate", "request_clarification", "stop_blocked"]
    tool_calls: Annotated[list[ToolCallProposal], Field(max_length=3)] = Field(default_factory=list)
    artifact_ids: list[Identifier] = Field(default_factory=list)
    message: str = ""

    @model_validator(mode="after")
    def validate_action(self):
        if self.kind == "call_tools" and not self.tool_calls:
            raise ValueError("call_tools requires at least one tool call")
        if self.kind != "call_tools" and self.tool_calls:
            raise ValueError("tool calls are allowed only for call_tools")
        if self.kind == "submit_candidate" and not self.artifact_ids:
            raise ValueError("submit_candidate requires artifacts")
        return self

class ArtifactRef(ContractModel):
    artifact_id: Identifier
    project_id: Identifier
    sha256: Digest
    byte_size: NonNegativeInt
    media_type: str
    classification: Literal["public", "project_private", "sensitive"] = "project_private"

class Citation(ContractModel):
    source_version_id: Identifier
    source_sha256: Digest
    start: NonNegativeInt
    end: Annotated[int, Field(strict=True, ge=1)]
    quote: Annotated[str, Field(min_length=1)]
    page: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def check_span(self):
        if self.end <= self.start:
            raise ValueError("citation end must be greater than start")
        return self

class OperationIntent(ContractModel):
    """Constructed by the trusted gateway, never accepted from an actor verbatim."""
    project_id: Identifier
    task_id: Identifier
    intent_id: Identifier
    tool_id: Identifier
    arguments: dict[str, JsonValue]
    resource_id: Identifier
    resource_version: str
    payload_sha256: Digest | None = None
    risk: RiskClass
    idempotency: IdempotencyClass
    max_cost_micro_usd: NonNegativeInt
    capability_id: Identifier

class ApprovalGrant(ContractModel):
    approval_id: Identifier
    project_id: Identifier
    task_id: Identifier
    intent_digest: Digest
    issued_to_owner: Identifier
    expires_at: AwareDatetime
    decision: Literal["approved", "rejected"]

class VerificationResult(ContractModel):
    criterion_id: Identifier
    verdict: Literal["PASS", "FAIL", "UNVERIFIABLE", "AWAITING_HUMAN"]
    artifact_sha256: Digest
    contract_sha256: Digest
    verifier_identity: Identifier
    evidence_ids: Annotated[list[Identifier], Field(min_length=1)]
    reason: str
    checked_at: AwareDatetime

class EventEnvelope(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: Identifier
    project_id: Identifier
    task_id: Identifier
    sequence: Annotated[int, Field(strict=True, ge=1)]
    event_type: Identifier
    occurred_at: AwareDatetime
    payload: dict[str, JsonValue]

class SearchRequest(ContractModel):
    project_id: Identifier
    query: Annotated[str, Field(min_length=1, max_length=4000)]
    mode: Literal["lexical", "dense", "hybrid"] = "hybrid"
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 8
    source_ids: list[Identifier] = Field(default_factory=list)

class SearchHit(ContractModel):
    chunk_id: Identifier
    document_id: Identifier
    text: str
    score: float
    citation: Citation

class SearchResult(ContractModel):
    hits: list[SearchHit]
    index_generation: Identifier
    embedding_revision: str | None
    degraded: bool = False
    degraded_reason: str | None = None

class ModelRequest(ContractModel):
    request_id: Identifier
    project_id: Identifier
    task_id: Identifier
    role: Literal["planner", "actor", "coder", "judge", "extractor"]
    input_artifact_ids: list[Identifier]
    output_schema_name: str | None
    max_output_tokens: Annotated[int, Field(strict=True, ge=1)]
    reservation_micro_usd: NonNegativeInt

class ModelResult(ContractModel):
    request_id: Identifier
    output: JsonValue
    usage_status: Literal["known", "unknown", "scripted"]
    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None
    actual_cost_micro_usd: NonNegativeInt | None
    provider_request_id: str | None

    @model_validator(mode="after")
    def known_usage(self):
        if self.usage_status == "known" and any(x is None for x in (self.input_tokens,self.output_tokens,self.actual_cost_micro_usd)):
            raise ValueError("known usage requires all accounting fields")
        if self.usage_status == "unknown" and self.actual_cost_micro_usd is not None:
            raise ValueError("unknown usage cannot pretend to have a known total cost")
        return self

class CodingJobRequest(ContractModel):
    project_id: Identifier
    task_id: Identifier
    intent_id: Identifier
    base_artifact: ArtifactRef
    objective: str
    contract_sha256: Digest
    allowed_tools: list[Identifier]
    deadline: AwareDatetime

class CodingJobResult(ContractModel):
    job_id: Identifier
    status: Literal["QUEUED", "STARTING", "ACTIVE", "EXPORTING", "FINISHED", "FAILED", "CANCELLED"]
    artifact: ArtifactRef | None = None
    patch: ArtifactRef | None = None
    event_artifact: ArtifactRef | None = None

class PromotionMetrics(ContractModel):
    independent_task_families: NonNegativeInt
    success_improvement: Annotated[float, Field(ge=-1, le=1)]
    paired_ci_lower: Annotated[float, Field(ge=-1, le=1)]
    cost_ratio: Annotated[float, Field(ge=0)] | None
    new_safety_failures: NonNegativeInt
    mandatory_regressions: NonNegativeInt
    evaluator_report_sha256: Digest

class SkillManifest(ContractModel):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    version: Annotated[int, Field(strict=True, ge=1)]
    project_id: Identifier
    parent_version: Annotated[int, Field(strict=True, ge=1)] | None = None
    bundle_sha256: Digest
    training_trace_ids: list[Identifier]
    required_tool_ids: list[Identifier]
    required_risk: RiskClass
    applicable_task_types: Annotated[list[str], Field(min_length=1)]
    status: Literal["DRAFT", "QUARANTINED", "STATIC_PASSED", "EVALUATING", "ELIGIBLE", "CANARY", "ACTIVE", "REJECTED", "ROLLED_BACK"]

SCHEMA_MODELS = [BudgetSpec,AcceptanceCriterion,GoalContract,TaskNode,Plan,ToolCallProposal,Decision,
    ArtifactRef,Citation,OperationIntent,ApprovalGrant,VerificationResult,EventEnvelope,
    SearchRequest,SearchHit,SearchResult,ModelRequest,ModelResult,CodingJobRequest,CodingJobResult,
    PromotionMetrics,SkillManifest]
