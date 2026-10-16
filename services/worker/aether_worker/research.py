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
_CITATION_LABEL = re.compile(
    r"(?i)\[\s*(?:资料|来源|source)?\s*\d+\s*\]"
    r"|【\s*(?:资料|来源|source)?\s*\d+\s*】"
    r"|(?:资料|来源|source)\s*\d+"
)


def estimate_tokens(text: str) -> int:
    """Budgeting count for the text we are about to send.

    cl100k is a local tokenizer, not the provider invoice. The larger of that
    count and the CJK character count is what the reservation must cover.
    """
    counted = len(_TOKEN_ENCODING.encode(text))
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return max(counted, cjk)


_CHAT_OVERHEAD_TOKENS = 8


def estimate_provider_request_tokens(prompt: str) -> int:
    """Count the chat request the gateway actually sends, not the bare prompt.

    ModelGateway.complete wraps the prompt as a user message. cl100k on the raw
    string undercounts that framing. The chat-shaped string plus a small slack
    is a local upper bound, not the provider's tokenizer invoice.
    """
    framed = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant"
    return estimate_tokens(framed) + _CHAT_OVERHEAD_TOKENS


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


def _substantive_numbers(answer: str) -> list[str]:
    """Numbers in the answer after citation labels such as 资料1 or [2] are removed."""
    return _numbers(_CITATION_LABEL.sub(" ", answer))


_UNITS = (
    "摄氏度",
    "华氏度",
    "开尔文",
    "毫升",
    "千克",
    "公里",
    "小时",
    "分钟",
    "秒钟",
    "毫米",
    "厘米",
    "次",
    "天",
    "升",
    "米",
    "克",
    "秒",
    "度",
)


def _cjk_bigrams(text: str) -> set[str]:
    chars = [char for char in text if "\u4e00" <= char <= "\u9fff"]
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


def _unit_span(text: str, end: int) -> tuple[str, int]:
    rest = text[end:]
    trimmed = rest.lstrip(" \t")
    offset = end + (len(rest) - len(trimmed))
    for unit in _UNITS:
        if trimmed.startswith(unit):
            return unit, offset + len(unit)
    return "", end


_PERIODS = ("毫秒", "分钟", "秒钟", "小时", "秒", "天", "年", "月", "周")
_POLARITY = (
    ("不超过", "ceiling"),
    ("不少于", "exceed"),
    ("不低于", "exceed"),
    ("至多", "ceiling"),
    ("至少", "exceed"),
    ("最多", "ceiling"),
    ("最少", "exceed"),
    ("上限", "ceiling"),
    ("下限", "exceed"),
    ("超过", "exceed"),
    ("高于", "exceed"),
    ("以内", "ceiling"),
)
_SUBJECT_NOISE = tuple(dict.fromkeys((*_PERIODS, *(word for word, _kind in _POLARITY), "接受", "发送", "允许", "可以", "请求", "完成", "一次", "每", "为", "的")))
_GOAL_PART_SPLIT = re.compile(r"以及|和|与")
_GENERIC_PART_WORDS = (
    "限额",
    "上限",
    "下限",
    "请求",
    "时间",
    "次数",
    "数量",
    "说明",
    "回答",
    "给出",
    "分别",
    "根据",
    "资料",
    "多少",
    "什么",
    "请",
    "周期",
    "分钟",
    "小时",
    "秒钟",
    "毫秒",
    "最多",
    "最少",
    "接受",
    "发送",
    "允许",
    "可以",
    "完成",
    "一次",
    "每",
    "为",
    "的",
)


def _rightmost(text: str, words: tuple[str, ...]) -> str:
    """Longest period word nearest the number. A short word inside a longer one does not win."""
    best = ""
    best_at = -1
    for word in sorted(set(words), key=len, reverse=True):
        at = text.rfind(word)
        if at < 0:
            continue
        if best and best_at <= at < best_at + len(best):
            continue
        if not best or at + len(word) >= best_at + len(best):
            best = word
            best_at = at
    return best


def _polarity(text: str) -> str:
    best = ""
    best_at = -1
    kind = ""
    for word, label in sorted(_POLARITY, key=lambda item: len(item[0]), reverse=True):
        at = text.rfind(word)
        if at < 0:
            continue
        if best and best_at <= at < best_at + len(best):
            continue
        if not best or at + len(word) >= best_at + len(best):
            best = word
            best_at = at
            kind = label
    return kind


def _subject_bigrams(prefix: str) -> frozenset[str]:
    cleaned = prefix
    for word in sorted(_SUBJECT_NOISE, key=len, reverse=True):
        cleaned = cleaned.replace(word, "")
    return frozenset(_cjk_bigrams(cleaned))


def _bindings(text: str) -> list[tuple[str, str, str, str, frozenset[str]]]:
    """Bind each number to its unit, period, comparison and local subject."""
    found: list[tuple[str, str, str, str, frozenset[str]]] = []
    for match in _NUMBER.finditer(text):
        unit, _unit_end = _unit_span(text, match.end())
        prefix = text[max(0, match.start() - 24) : match.start()]
        for separator in ("\n", "。", "；", "，", "、", "：", "“", "”", "\"", ":"):
            if separator in prefix:
                prefix = prefix.rsplit(separator, 1)[-1]
        found.append((match.group(), unit, _rightmost(prefix, _PERIODS), _polarity(prefix), _subject_bigrams(prefix)))
    return found


def _binding_covered(
    binding: tuple[str, str, str, str, frozenset[str]],
    answers: list[tuple[str, str, str, str, frozenset[str]]],
    *,
    require_subject: bool,
) -> bool:
    number, unit, period, polarity, subjects = binding
    for other_number, other_unit, other_period, other_polarity, other_subjects in answers:
        if other_number != number or other_unit != unit:
            continue
        if period and period != other_period:
            continue
        if polarity and other_polarity and polarity != other_polarity:
            continue
        if subjects and not other_subjects and require_subject:
            continue
        if not other_subjects <= subjects:
            continue
        return True
    return False


def _part_objects(part: str) -> set[str]:
    cleaned = part
    for word in sorted(_GENERIC_PART_WORDS, key=len, reverse=True):
        cleaned = cleaned.replace(word, "")
    return _tokens(cleaned)


def _unsupported_goal_part(goal: str, facts: list[str]) -> bool:
    """Each requested object needs its own source support, not a shared generic word."""
    parts = [part.strip() for part in _GOAL_PART_SPLIT.split(goal) if len(_cjk_bigrams(part)) >= 1]
    if len(parts) < 2:
        return False
    covered: set[str] = set()
    for fact in facts:
        covered |= _part_objects(fact)
    for part in parts:
        objects = _part_objects(part)
        if not objects or not objects & covered:
            return True
    return False


def _covers(answer: str, fact: str, *, require_subject: bool = False) -> bool:
    if fact in answer:
        return True
    bindings = _bindings(fact)
    if not bindings:
        return False
    answers = _bindings(_CITATION_LABEL.sub(" ", answer))
    return all(_binding_covered(binding, answers, require_subject=require_subject) for binding in bindings)


def _requires_subject(facts: list[str]) -> bool:
    named = 0
    for fact in facts:
        if any(binding[4] for binding in _bindings(fact)):
            named += 1
    return named >= 2


def fact_satisfied(answer: str, fact: str, goal: str) -> bool:
    """The answer carries this fact's values and does not replace them."""
    allowed = set(_numbers(fact)) | set(_numbers(goal))
    if any(number not in allowed for number in _substantive_numbers(answer)):
        return False
    return _covers(answer, fact)


def goal_completion_detail(*, answer: str, goal: str, source_text: str) -> str:
    """Empty when the goal is satisfied. Otherwise the check that failed."""
    facts = required_facts(source_text=source_text, goal=goal)
    if not facts:
        return "no_supported_fact"
    if not answer or answer.strip() == goal.strip():
        return "empty_answer"
    allowed = set(_numbers(goal))
    for fact in facts:
        allowed.update(_numbers(fact))
    extras: list[str] = []
    for number in _substantive_numbers(answer):
        if number not in allowed and number not in extras:
            extras.append(number)
    if extras:
        return "unsupported_number:" + ",".join(f"'{number}'" for number in extras)
    if _unsupported_goal_part(goal, facts):
        return "unsupported_goal_part"
    require_subject = _requires_subject(facts)
    if any(not _covers(answer, fact, require_subject=require_subject) for fact in facts):
        return "missing_fact"
    return ""


def goal_completion_state(*, answer: str, goal: str, source_text: str) -> str:
    """Separate goal satisfaction from a short citation overlap."""
    detail = goal_completion_detail(answer=answer, goal=goal, source_text=source_text)
    if detail in {"no_supported_fact", "unsupported_goal_part"}:
        return "unverifiable"
    if detail:
        return "fail"
    return "pass"


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
