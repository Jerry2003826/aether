"""Scripted research step and independent verification. The model call is not live."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

import tiktoken

from sqlalchemy import text
from temporalio import activity

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database


EFFECTS: dict[str, Any] = {}


@activity.defn
async def record_research_effect(payload: dict) -> int:
    return record_command_effect(
        EFFECTS["database"],
        owner_id=str(EFFECTS["owner_id"]),
        project_id=str(EFFECTS["project_id"]),
        task_id=str(payload["task_id"]),
        command_id=str(payload["command_id"]),
    )


def read_local_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("local_text_rejected") from exc


_EXCERPT_MARKERS = ("资料", "摘录", "根据")


def _outside_excerpt(goal: str) -> bool:
    """The offline provider only quotes a source line. It does not calculate."""
    if "计算" in goal:
        return True
    return re.search(r"\d+\s*[×xX*]\s*\d+", goal) is not None


def _excerpt_request(goal: str) -> bool:
    return any(marker in goal for marker in _EXCERPT_MARKERS)


def _tokens(text: str) -> set[str]:
    found = {word.lower() for word in re.findall(r"[A-Za-z0-9]{2,}", text)}
    chars: list[str] = []

    def flush() -> None:
        if len(chars) >= 2:
            found.update(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
        chars.clear()

    for character in text:
        if "\u4e00" <= character <= "\u9fff":
            chars.append(character)
        else:
            flush()
    flush()
    return found


def offline_fact(*, source_text: str, goal: str) -> str:
    """Pick one source line an offline excerpt may quote.

    This is not a live model. A goal it cannot address, including arithmetic,
    returns an empty string so the task stays unverified.
    """
    goal_text = goal.strip()
    lines: list[str] = []
    for line in source_text.splitlines():
        fact = line.strip()
        if len(fact) < 8 or fact == goal_text or fact in goal_text:
            continue
        lines.append(fact)
    if not lines or _outside_excerpt(goal_text):
        return ""
    ranked = sorted((len(_tokens(goal_text) & _tokens(fact)), index, fact) for index, fact in enumerate(lines))
    best_score, _index, best = max(ranked, key=lambda item: (item[0], -item[1]))
    if best_score > 0:
        return best
    if len(lines) == 1 and _excerpt_request(goal_text):
        return lines[0]
    return ""


def research_prompt(*, goal: str, sources: list[str]) -> str:
    """Ground a live model in the allowed sources. Disallowed text must not be added here."""
    blocks = [f"[资料{index}]\n{source.strip()}" for index, source in enumerate(sources, start=1)]
    return "只根据下列资料回答目标。回答必须包含你所依据的资料原句，不要使用资料之外的事实。\n" f"目标：{goal.strip()}\n\n" + "\n\n".join(blocks)


def cites_fact(answer: str, fact: str) -> bool:
    """Citation clue only. A 12-character span does not prove the goal is done."""
    if len(fact) < 8 or not answer:
        return False
    if fact in answer:
        return True
    if len(fact) < 12:
        return False
    return any(fact[start : start + 12] in answer for start in range(0, len(fact) - 11))


_TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
_NUMBER = re.compile(r"\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万亿]+")


def estimate_tokens(text: str) -> int:
    """Budgeting count for the text we are about to send.

    cl100k is a local tokenizer, not the provider invoice. The larger of that
    count and the CJK character count is what the reservation must cover.
    """
    counted = len(_TOKEN_ENCODING.encode(text))
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return max(counted, cjk)


def required_facts(*, source_text: str, goal: str) -> list[str]:
    """Every source line that overlaps the goal. One overlap is not the whole question."""
    goal_text = goal.strip()
    lines: list[str] = []
    for line in source_text.splitlines():
        fact = line.strip()
        if len(fact) < 8 or fact == goal_text or fact in goal_text:
            continue
        lines.append(fact)
    if not lines or _outside_excerpt(goal_text):
        return []
    overlapping = [fact for fact in lines if _tokens(goal_text) & _tokens(fact)]
    if overlapping:
        return overlapping
    if len(lines) == 1 and _excerpt_request(goal_text):
        return [lines[0]]
    return []


def _numbers(text: str) -> list[str]:
    return _NUMBER.findall(text)


def _covers(answer: str, fact: str) -> bool:
    if fact in answer:
        return True
    numbers = _numbers(fact)
    return bool(numbers) and all(number in _numbers(answer) for number in numbers)


def fact_satisfied(answer: str, fact: str, goal: str) -> bool:
    """The answer carries this fact's values and does not replace them."""
    allowed = set(_numbers(fact)) | set(_numbers(goal))
    if any(number not in allowed for number in _numbers(answer)):
        return False
    return _covers(answer, fact)


def goal_completion_state(*, answer: str, goal: str, source_text: str) -> str:
    """Separate goal satisfaction from a short citation overlap."""
    facts = required_facts(source_text=source_text, goal=goal)
    if not facts:
        return "unverifiable"
    if not answer or answer.strip() == goal.strip():
        return "fail"
    allowed = set(_numbers(goal))
    for fact in facts:
        allowed.update(_numbers(fact))
    if any(number not in allowed for number in _numbers(answer)):
        return "fail"
    if all(_covers(answer, fact) for fact in facts):
        return "pass"
    return "fail"


def scripted_answer(*, source_text: str, goal: str) -> str:
    """Offline excerpt stand-in. It is not a live model and it does not return the goal unchanged."""
    fact = offline_fact(source_text=source_text, goal=goal)
    if not fact:
        return ""
    answer = f"资料记载：{fact}"
    if answer.strip() == goal.strip():
        return ""
    return answer


def scripted_research(*, store: ArtifactStore, gateway: Any, token: str, owner_id: str, project_id: str, artifact_id: str) -> dict[str, Any]:
    data, _media = store.get(owner_id=owner_id, project_id=project_id, artifact_id=artifact_id)
    digest = hashlib.sha256(data).hexdigest()
    extracted = read_local_text(data)
    model = gateway.complete(token, {"alias": "actor", "prompt": extracted})
    return {
        "artifact_sha256": digest,
        "tool_text": extracted,
        "model_output": model.get("output"),
        "model_mode": "scripted",
        "usage_status": model.get("usage_status"),
        "gateway_calls": 1,
    }


def record_command_effect(database: Database, *, owner_id: str, project_id: str, task_id: str, command_id: str) -> int:
    event_key = f"research-{command_id}"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        existing = conn.execute(
            text("SELECT 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_key = :event_key"),
            {"project_id": project_id, "task_id": task_id, "event_key": event_key},
        ).first()
        if existing is None:
            sequence = conn.execute(
                text("SELECT coalesce(max(sequence), 0) + 1 FROM task_events WHERE project_id = :project_id AND task_id = :task_id"),
                {"project_id": project_id, "task_id": task_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO task_events (project_id, task_id, sequence, event_key, event_type, payload) "
                    "VALUES (:project_id, :task_id, :sequence, :event_key, 'research.call', CAST(:payload AS jsonb))"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "sequence": sequence,
                    "event_key": event_key,
                    "payload": '{"calls": 1}',
                },
            )
        count = conn.execute(
            text("SELECT count(*) FROM task_events WHERE project_id = :project_id AND task_id = :task_id AND event_type = 'research.call'"),
            {"project_id": project_id, "task_id": task_id},
        ).scalar_one()
    return int(count)


def record_verification(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    task_id: str,
    contract_id: str,
    artifact_id: str,
    report_artifact_id: str,
    criterion_id: str,
    artifact_sha256: str,
    observed_sha256: str,
    contract_sha256: str,
    expires_at: datetime,
    now: datetime,
    actor_verdict: str | None = None,
    checks_passed: bool = True,
    outcome: str | None = None,
    detail: str | None = None,
    source_snapshot_sha256: str | None = None,
    verifier_version: str | None = None,
    conn: Any = None,
) -> dict[str, str]:
    if expires_at <= now or artifact_sha256 != observed_sha256:
        verdict = "FAIL"
        reason = "候选已过期或哈希不一致"
    elif outcome == "unverifiable":
        verdict = "UNVERIFIABLE"
        reason = detail or "离线脚本不能确认目标已完成"
    elif outcome == "fail" or not checks_passed:
        verdict = "FAIL"
        reason = detail or "独立检查未通过"
    else:
        verdict = "PASS"
        reason = detail or "哈希一致且候选未过期"
    if actor_verdict is not None and actor_verdict != verdict:
        reason = f"{reason}；已忽略 actor 给出的 {actor_verdict}"

    def _insert(active: Any) -> tuple[Any, Any]:
        identity = active.execute(text("SELECT current_user")).scalar_one()
        verification_id = active.execute(
            text(
                "INSERT INTO verification_results (project_id, task_id, contract_id, artifact_id, criterion_id, verdict, "
                "artifact_sha256, contract_sha256, verifier_identity, report_artifact_id, reason, checked_at, "
                "observed_sha256, source_snapshot_sha256, verifier_version) "
                "VALUES (:project_id, :task_id, :contract_id, :artifact_id, :criterion_id, :verdict, :artifact_sha256, "
                ":contract_sha256, :identity, :report_artifact_id, :reason, now(), :observed_sha256, "
                ":source_snapshot_sha256, :verifier_version) RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "contract_id": contract_id,
                "artifact_id": artifact_id,
                "criterion_id": criterion_id,
                "verdict": verdict,
                "artifact_sha256": artifact_sha256,
                "contract_sha256": contract_sha256,
                "identity": identity,
                "report_artifact_id": report_artifact_id,
                "reason": reason,
                "observed_sha256": observed_sha256,
                "source_snapshot_sha256": source_snapshot_sha256,
                "verifier_version": verifier_version,
            },
        ).scalar_one()
        return identity, verification_id

    if conn is None:
        with database.transaction(owner_id=owner_id, project_id=project_id) as owned:
            identity, verification_id = _insert(owned)
    else:
        identity, verification_id = _insert(conn)
    return {"id": str(verification_id), "verdict": verdict, "reason": reason, "verifier": str(identity)}
