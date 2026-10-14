"""Pure reference algorithms with deliberate limits.

Production must add database transactions, trusted identity verification, actual
network enforcement and Temporal integration. These helpers alone are not a
security boundary and do not establish the safety of an agent deployment.
"""
from __future__ import annotations
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
from .models import (ApprovalGrant, Citation, GoalContract, IdempotencyClass,
                     OperationIntent, OperationState, Plan, PromotionMetrics,
                     TaskState, VerificationResult)

def canonical_sha256(value: Any) -> str:
    """Hash schema-normalized JSON, not arbitrary Python objects or NaN."""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()

def contract_digest(contract: GoalContract) -> str:
    return canonical_sha256(contract.model_dump(mode="json"))

def intent_digest(intent: OperationIntent) -> str:
    return canonical_sha256(intent.model_dump(mode="json"))

def validate_plan(plan: Plan, max_depth: int = 32) -> list[str]:
    """Return deterministic topological order; reject missing, duplicate or cyclic dependencies."""
    nodes = {node.node_id: node for node in plan.nodes}
    if len(nodes) != len(plan.nodes):
        raise ValueError("duplicate node ID")
    for node in plan.nodes:
        if len(set(node.dependencies)) != len(node.dependencies):
            raise ValueError("duplicate dependency")
        if not set(node.dependencies).issubset(nodes):
            raise ValueError("missing dependency")
    result: list[str] = []
    depth: dict[str, int] = {}
    pending = set(nodes)
    while pending:
        ready = sorted(n for n in pending if set(nodes[n].dependencies).issubset(depth))
        if not ready:
            raise ValueError("cyclic dependencies")
        for node_id in ready:
            depth[node_id] = 1 + max((depth[d] for d in nodes[node_id].dependencies), default=0)
            if depth[node_id] > max_depth:
                raise ValueError("plan depth exceeds limit")
            result.append(node_id)
            pending.remove(node_id)
    return result

def ready_nodes(plan: Plan, verified_pass: set[str], active: set[str]) -> list[str]:
    validate_plan(plan)
    known = {n.node_id for n in plan.nodes}
    if not (verified_pass | active).issubset(known):
        raise ValueError("unknown node state")
    return sorted(n.node_id for n in plan.nodes if n.node_id not in verified_pass | active
                  and set(n.dependencies).issubset(verified_pass))[:max(0,plan.max_parallel-len(active))]

_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.DRAFT: {TaskState.QUEUED,TaskState.CANCELLED},
    TaskState.QUEUED: {TaskState.RUNNING,TaskState.CANCELLED,TaskState.FAILED},
    TaskState.RUNNING: {TaskState.VERIFYING,TaskState.WAITING_APPROVAL,TaskState.PAUSED,TaskState.NEEDS_REVIEW,TaskState.FAILED,TaskState.CANCELLED},
    TaskState.VERIFYING: {TaskState.SUCCEEDED,TaskState.RUNNING,TaskState.WAITING_APPROVAL,TaskState.PAUSED,TaskState.NEEDS_REVIEW,TaskState.FAILED,TaskState.CANCELLED},
    TaskState.WAITING_APPROVAL: {TaskState.RUNNING,TaskState.VERIFYING,TaskState.PAUSED,TaskState.FAILED,TaskState.CANCELLED},
    TaskState.PAUSED: {TaskState.RUNNING,TaskState.VERIFYING,TaskState.CANCELLED},
    TaskState.NEEDS_REVIEW: {TaskState.RUNNING,TaskState.VERIFYING,TaskState.FAILED,TaskState.CANCELLED},
    TaskState.SUCCEEDED:set(), TaskState.FAILED:set(), TaskState.CANCELLED:set(),
}

def allowed_transition(old: TaskState, new: TaskState) -> bool:
    """Shape-only transition check; authorization and saved resume_state are mandatory elsewhere."""
    return new in _TRANSITIONS[old]

def approval_matches(intent: OperationIntent, grant: ApprovalGrant, now: datetime,
                     authenticated_owner: str, consumed_ids: set[str]) -> bool:
    """Use only a grant loaded from the trusted DB after authenticating the owner.

Production needs a transactional consume and signature/identity checks; the set
here is intentionally just an in-memory test fixture.
"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    return (grant.decision == "approved" and grant.approval_id not in consumed_ids
            and grant.project_id == intent.project_id and grant.task_id == intent.task_id
            and grant.issued_to_owner == authenticated_owner and grant.expires_at > now
            and hmac.compare_digest(grant.intent_digest,intent_digest(intent)))

def recovery_action(state: OperationState, kind: IdempotencyClass) -> str:
    if state == OperationState.CONFIRMED:
        return "return_existing"
    if state == OperationState.PREPARED:
        return "dispatch_once_under_lease"
    if state == OperationState.FAILED:
        return "inspect_failure_before_retry"
    if kind == IdempotencyClass.READ_ONLY:
        return "retry_read_with_new_cost_reservation_if_billable"
    if kind == IdempotencyClass.IDEMPOTENT_WRITE:
        return "query_then_retry_same_provider_key"
    if kind == IdempotencyClass.RECONCILABLE_WRITE:
        return "reconcile_before_any_retry"
    return "manual_review_no_automatic_retry"

def reciprocal_rank_fusion(rankings: Iterable[Iterable[str]], k: int = 60) -> list[tuple[str,float]]:
    if k <= 0:
        raise ValueError("RRF k must be positive")
    scores: dict[str,float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        unique_rank = 0
        for item in ranking:
            if item in seen:
                continue
            seen.add(item)
            unique_rank += 1
            scores[item] = scores.get(item,0.0) + 1.0/(k+unique_rank)
    return sorted(scores.items(),key=lambda row:(-row[1],row[0]))

def citation_matches(text: str, citation: Citation) -> bool:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest,citation.source_sha256) and 0 <= citation.start < citation.end <= len(text) and text[citation.start:citation.end] == citation.quote

def verified_completion(contract: GoalContract, artifact_sha256: str,
                        results: list[VerificationResult],
                        trusted_evidence: Mapping[str, str]) -> bool:
    """Validate complete current-digest PASS evidence from a trusted evidence registry.

trusted_evidence maps persisted evidence IDs to their authenticated verifier
identity, loaded by the trusted service, never supplied by the actor/API caller.
Semantic correctness still depends on what each independent verifier checks.
"""
    expected = contract_digest(contract)
    required = {c.criterion_id for c in contract.criteria if c.mandatory}
    by_id: dict[str,list[VerificationResult]] = {}
    for result in results:
        by_id.setdefault(result.criterion_id,[]).append(result)
    for criterion in required:
        items = by_id.get(criterion,[])
        # Caller selects the current verification attempt; ambiguous duplicates fail closed.
        if len(items) != 1:
            return False
        item = items[0]
        if item.verdict != "PASS" or item.artifact_sha256 != artifact_sha256 or item.contract_sha256 != expected:
            return False
        if not all(trusted_evidence.get(e) == item.verifier_identity for e in item.evidence_ids):
            return False
    return bool(required)

@dataclass
class BudgetLedger:
    """Single-process reference only. Production requires database row locks/CAS."""
    limit: int
    spent: int = 0
    held: dict[str,int] = field(default_factory=dict)
    settled: dict[str,int] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)

    def __post_init__(self):
        if type(self.limit) is not int or self.limit < 0 or type(self.spent) is not int or self.spent < 0:
            raise ValueError("budget values must be nonnegative integers")

    @property
    def available(self) -> int:
        return self.limit-self.spent-sum(self.held.values())

    def reserve(self, request_id: str, amount: int) -> bool:
        if type(amount) is not int or amount < 0:
            raise ValueError("reservation must be a nonnegative integer")
        if request_id in self.settled:
            raise ValueError("settled request IDs cannot be reused")
        if request_id in self.held:
            if self.held[request_id] != amount:
                raise ValueError("idempotency key payload changed")
            return True
        if amount > self.available:
            return False
        self.held[request_id]=amount
        return True

    def mark_unknown(self, request_id: str) -> None:
        if request_id not in self.held:
            raise KeyError(request_id)
        self.unknown.add(request_id)

    def settle(self, request_id: str, actual: int) -> None:
        if type(actual) is not int or actual < 0:
            raise ValueError("actual cost must be a nonnegative integer")
        if request_id in self.settled:
            if self.settled[request_id] != actual:
                raise ValueError("conflicting settlement")
            return
        if request_id not in self.held:
            raise KeyError(request_id)
        del self.held[request_id]
        self.unknown.discard(request_id)
        self.settled[request_id]=actual
        self.spent += actual  # Record the truth even when provider cost exceeded reservation.

    def release_proven_not_sent(self, request_id: str, *, proof_not_sent: bool) -> None:
        if not proof_not_sent or request_id in self.unknown:
            raise ValueError("ambiguous billed requests must retain reservation")
        if request_id not in self.held:
            raise KeyError(request_id)
        del self.held[request_id]


def promotion_gate(metrics: PromotionMetrics) -> str:
    if metrics.new_safety_failures or metrics.mandatory_regressions:
        return "REJECTED"
    if metrics.independent_task_families < 30 or metrics.cost_ratio is None:
        return "INSUFFICIENT_EVIDENCE"
    if metrics.success_improvement < 0.05 or metrics.paired_ci_lower <= 0:
        return "INSUFFICIENT_EVIDENCE"
    if metrics.cost_ratio > 1.10:
        return "REJECTED"
    return "ELIGIBLE_NOT_DEPLOYED"

def epsilon_greedy_probability(selected_id: str, best_id: str,
                               eligible_ids: list[str], epsilon: float) -> float:
    if not eligible_ids or len(set(eligible_ids)) != len(eligible_ids):
        raise ValueError("candidate set must be nonempty and unique")
    if best_id not in eligible_ids or selected_id not in eligible_ids or not 0<=epsilon<=1:
        raise ValueError("invalid choice or epsilon")
    return epsilon/len(eligible_ids) + (1-epsilon if selected_id==best_id else 0)

def validate_relative_export_path(value: str) -> str:
    """Lexical validation ONLY; production must separately prevent symlink/race escapes."""
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("invalid export path")
    p=PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or str(p)==".":
        raise ValueError("path escape")
    return str(p)
