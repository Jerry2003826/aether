"""Typed DAG checks. This module is deterministic and performs no I/O."""

from __future__ import annotations

from reference.core import ready_nodes, validate_plan
from reference.models import Plan, TaskNode

WRITE_ROLES = {"coding", "extraction"}


def compile_plan(nodes: list[dict], *, max_depth: int = 32) -> list[str]:
    if len(nodes) > 32:
        raise ValueError("plan node limit")
    plan = Plan(nodes=[TaskNode(**node) for node in nodes], max_parallel=2)
    return validate_plan(plan, max_depth=max_depth)


def schedule_ready(nodes: list[dict], verified: set[str], active: set[str]) -> list[str]:
    plan = Plan(nodes=[TaskNode(**node) for node in nodes], max_parallel=2)
    by_id = {node.node_id: node for node in plan.nodes}
    chosen: list[str] = []
    active_writes = {resource for node_id in active if by_id[node_id].role in WRITE_ROLES for resource in by_id[node_id].resource_ids}
    active_reads = sum(1 for node_id in active if by_id[node_id].role not in WRITE_ROLES)
    selected_reads = 0
    for node_id in ready_nodes(plan, verified, active | set(chosen)):
        node = by_id[node_id]
        if node.role in WRITE_ROLES:
            if any(resource in active_writes for resource in node.resource_ids):
                continue
            active_writes.update(node.resource_ids)
            chosen.append(node_id)
            continue
        if active_reads + selected_reads >= 2:
            continue
        selected_reads += 1
        chosen.append(node_id)
    return chosen


def accept_actor_decision(payload: dict) -> dict:
    if payload.get("kind") == "mark_success":
        raise ValueError("actor cannot mark success")
    from reference.models import Decision

    return Decision.model_validate(payload).model_dump()
