"""Live model probe. Missing configuration is blocked, never a scripted pass.

At most eight short chat-completions calls. Hybrid retrieval uses the measured
BGE-M3 vectors and a fresh BM25 index. Linux isolation runs only when Docker
answers; otherwise that check stays not_run.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aether_worker.retrieval import EMBEDDING_REVISION, RetrievalError, _index_bm25, _lexical_ids, encode_texts, rrf

LIVE_KEY = "AETHER_LIVE_MODEL_KEY"
LIVE_BASE = "AETHER_LIVE_MODEL_BASE"
LIVE_ALIAS = "AETHER_LIVE_MODEL_ALIAS"
NOT_RUN = "not_run"
MAX_CALLS = 8
MAX_OUTPUT_TOKENS = 128
MAX_TOTAL_TOKENS = 4000
RESEARCH_TOOL = "cite_finding"
CODING_TOOL = "submit_function"
QUERY = "苹果派"
GOLD_ID = "chunk-apple"
CLAIM_MARK = "一百八十"
CORPUS = (
    {"id": "chunk-apple", "text": "苹果派的烘烤温度是一百八十度"},
    {"id": "chunk-rail", "text": "The railway timetable lists the midnight express to York"},
    {"id": "chunk-code", "text": "assert left != right && flag == true"},
)
ISOLATION_IMAGE = "python:3.12-alpine"
ISOLATION_FLAGS = ("--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "32")
ISOLATION_SCRIPT = (
    "import os,socket\n"
    "print('key', 'AETHER_LIVE_MODEL_KEY' in os.environ)\n"
    "sock=socket.socket()\n"
    "sock.settimeout(2)\n"
    "try:\n"
    " sock.connect(('1.1.1.1', 443))\n"
    " print('net open')\n"
    "except OSError as exc:\n"
    " print('net', exc.errno)\n"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(req.full_url, code, "redirect_blocked", headers, fp)


def required_environment() -> list[str]:
    return [LIVE_KEY, LIVE_BASE, LIVE_ALIAS]


def redact_secret(value: str, secret: str) -> str:
    if not secret or not value:
        return value
    return value.replace(secret, "[REDACTED]")


def _redact_tree(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return redact_secret(value, secret)
    if isinstance(value, list):
        return [_redact_tree(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact_tree(item, secret) for key, item in value.items()}
    return value


def _blocked(missing: list[str]) -> dict[str, Any]:
    return {
        "status": "blocked",
        "live_ready": False,
        "missing": missing,
        "research": NOT_RUN,
        "coding": NOT_RUN,
        "hybrid_retrieval": NOT_RUN,
        "linux_isolation": NOT_RUN,
        "scripted_substitute": False,
        "checks": [],
        "calls": 0,
        "caps": _caps(),
    }


def _caps() -> dict[str, int]:
    return {"max_calls": MAX_CALLS, "max_output_tokens": MAX_OUTPUT_TOKENS, "max_total_tokens": MAX_TOTAL_TOKENS}


def _usage(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "cost_micro_usd": None, "cost_status": "unknown"}
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    total = raw.get("total_tokens")
    if not isinstance(total, int) and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion
    cost = raw.get("cost_micro_usd", raw.get("cost"))
    cost_micro = cost if isinstance(cost, int) else None
    return {
        "prompt_tokens": prompt if isinstance(prompt, int) else None,
        "completion_tokens": completion if isinstance(completion, int) else None,
        "total_tokens": total if isinstance(total, int) else None,
        "cost_micro_usd": cost_micro,
        "cost_status": "known" if cost_micro is not None else "unknown",
    }


def _tool_call(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    try:
        message = payload["choices"][0]["message"]
        call = message["tool_calls"][0]["function"]
        name = call["name"]
        raw = call["arguments"]
    except (KeyError, IndexError, TypeError):
        return None, None
    if not isinstance(name, str):
        return None, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return name, None
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return name, None
    return name, parsed if isinstance(parsed, dict) else None


def _tool_spec(name: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        },
    }


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _passages() -> list[dict[str, Any]]:
    return [
        {"id": item["id"], "text": item["text"], "start_offset": 0, "end_offset": len(item["text"])}
        for item in CORPUS
    ]


def _hybrid_context() -> dict[str, Any]:
    """BM25 plus measured dense vectors. A missing encoder stays not_run."""
    passages = _passages()
    try:
        vectors = {item["id"]: encode_texts([item["text"]])[0] for item in passages}
        query_vector = encode_texts([QUERY])[0]
    except RetrievalError as exc:
        return {"status": NOT_RUN, "reason": str(exc), "passages": [], "gold_id": GOLD_ID}
    dense_ids = [
        item["id"]
        for item in sorted(passages, key=lambda item: (-_cosine(query_vector, vectors[item["id"]]), item["id"]))
    ]
    with tempfile.TemporaryDirectory() as temporary:
        index = Path(temporary) / "bm25"
        _index_bm25([item["text"] for item in passages], index)
        bm25_ids = _lexical_ids(index, [item["id"] for item in passages], QUERY)
    fused = rrf([bm25_ids, dense_ids])
    ok = bool(bm25_ids) and bool(dense_ids) and GOLD_ID in bm25_ids and GOLD_ID in dense_ids and bool(fused) and fused[0] == GOLD_ID
    return {
        "status": "pass" if ok else "fail",
        "reason": None if ok else "gold_not_first",
        "query": QUERY,
        "embedding_revision": EMBEDDING_REVISION,
        "gold_id": GOLD_ID,
        "passages": passages,
        "bm25": bm25_ids,
        "dense": dense_ids,
        "rrf": fused,
    }


def _context_lines(passages: list[dict[str, Any]]) -> str:
    lines = []
    for item in passages:
        lines.append(f"{item['id']} offset {item['start_offset']}-{item['end_offset']}: {item['text']}")
    return "\n".join(lines)


def _research_body(model: str, passages: list[dict[str, Any]]) -> dict[str, Any]:
    listed = _context_lines(passages)
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "tool_choice": {"type": "function", "function": {"name": RESEARCH_TOOL}},
        "tools": [
            _tool_spec(
                RESEARCH_TOOL,
                {
                    "claim": {"type": "string"},
                    "citation_id": {"type": "string"},
                    "start_offset": {"type": "integer"},
                    "end_offset": {"type": "integer"},
                },
                ["claim", "citation_id", "start_offset", "end_offset"],
            )
        ],
        "messages": [
            {
                "role": "user",
                "content": (
                    "只根据下列混合检索结果调用 cite_finding。不要使用结果以外的知识。"
                    "citation_id、start_offset、end_offset 必须与选中条目一致。claim 只写该条目里的事实。\n"
                    + listed
                ),
            }
        ],
    }


def _protocol_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "tool_choice": {"type": "function", "function": {"name": RESEARCH_TOOL}},
        "tools": [
            _tool_spec(
                RESEARCH_TOOL,
                {"claim": {"type": "string"}, "citation_id": {"type": "string"}},
                ["claim", "citation_id"],
            )
        ],
        "messages": [{"role": "user", "content": f"调用 {RESEARCH_TOOL}。claim 写 pong，citation_id 写 none。"}],
    }


def _coding_body(model: str, *, repair: str | None) -> dict[str, Any]:
    if repair is None:
        instruction = f"只调用 {CODING_TOOL}。source 是 def add(a, b):，函数体只有 return a - b。不要 import。"
    else:
        instruction = (
            f"只调用 {CODING_TOOL}。独立测试没有通过：{repair}。"
            "请提交新的 source，函数仍是 def add(a, b):，函数体只有 return a + b。不要复用上次源码，不要 import。"
        )
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "tool_choice": {"type": "function", "function": {"name": CODING_TOOL}},
        "tools": [_tool_spec(CODING_TOOL, {"source": {"type": "string"}}, ["source"])],
        "messages": [{"role": "user", "content": instruction}],
    }


def _isolation_command() -> list[str]:
    return ["docker", "run", "--rm", *ISOLATION_FLAGS, ISOLATION_IMAGE, "python", "-c", ISOLATION_SCRIPT]


def _network_errno(stdout: str) -> int | None:
    for line in stdout.splitlines():
        if not line.startswith("net ") or line == "net open":
            continue
        piece = line.split(" ", 1)[1]
        if piece.lstrip("-").isdigit():
            return int(piece)
    return None


def _linux_isolation(secret: str) -> dict[str, Any]:
    if sys.platform != "linux":
        return {"status": NOT_RUN, "reason": "not_linux"}
    if shutil.which("docker") is None:
        return {"status": NOT_RUN, "reason": "docker_missing"}
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if info.returncode != 0:
        return {"status": NOT_RUN, "reason": "docker_unavailable"}
    command = _isolation_command()
    if secret and any(secret in part for part in command):
        return {"status": "fail", "reason": "secret_in_command"}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": NOT_RUN, "reason": redact_secret(type(exc).__name__, secret)}
    output = redact_secret((completed.stdout or "") + (completed.stderr or ""), secret)
    evidence = {
        "image": ISOLATION_IMAGE,
        "flags": list(ISOLATION_FLAGS),
        "exit_code": completed.returncode,
        "network_errno": _network_errno(completed.stdout or ""),
        "secret_present": "key True" in (completed.stdout or ""),
    }
    if completed.returncode != 0:
        reason = "image_unavailable" if "Unable to find image" in output or "pull access denied" in output else "container_failed"
        evidence["status"] = NOT_RUN if reason == "image_unavailable" else "fail"
        evidence["reason"] = reason
        return evidence
    secret_absent = "key False" in (completed.stdout or "")
    network_blocked = evidence["network_errno"] is not None and "net open" not in (completed.stdout or "")
    if secret_absent and network_blocked:
        evidence["status"] = "pass"
        evidence["reason"] = None
        return evidence
    evidence["status"] = "fail"
    evidence["reason"] = "isolation_incomplete"
    return evidence


def _post(url: str, body: dict[str, Any], key: str, timeout: float) -> tuple[int, dict[str, Any], int]:
    raw = json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=raw,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(redact_secret(f"http_{exc.code}:{detail}", key)) from None
    except Exception as exc:
        raise RuntimeError(redact_secret(f"{type(exc).__name__}:{exc}", key)) from None
    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("response_not_json") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("response_not_object")
    return status, decoded, latency_ms


def _sha(source: str | None) -> str | None:
    if source is None:
        return None
    return hashlib.sha256(source.encode()).hexdigest()


def _call_row(call_id: str, kind: str, latency_ms: int, usage: dict[str, Any], source: str | None) -> dict[str, Any]:
    return {
        "id": call_id,
        "kind": kind,
        "latency_ms": latency_ms,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "submission_sha256": _sha(source),
    }


def _check(check_id: str, status: str, latency_ms: int | None, usage: dict[str, Any] | None, reason: str | None, *, call_id: str | None = None) -> dict[str, Any]:
    row = {"id": check_id, "status": status, "latency_ms": latency_ms, "reason": reason}
    if call_id is not None:
        row["call_id"] = call_id
    if usage is None:
        row.update({"prompt_tokens": None, "completion_tokens": None, "cost_micro_usd": None, "cost_status": "unknown"})
    else:
        row.update(
            {
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "cost_micro_usd": usage["cost_micro_usd"],
                "cost_status": usage["cost_status"],
            }
        )
    return row


def _apply_binop(op: ast.operator, left: str, right: str, a: int, b: int) -> int | None:
    values = {"a": a, "b": b}
    if left not in values or right not in values:
        return None
    if isinstance(op, ast.Add):
        return values[left] + values[right]
    if isinstance(op, ast.Sub):
        return values[left] - values[right]
    return None


def _add_source_ok(source: str) -> tuple[bool, str]:
    """Independent test: add(1, 2) == 3 and add(-4, 4) == 0. The reason is the observed result."""
    if len(source) > 200:
        return False, "source_too_long"
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False, "syntax"
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return False, "shape"
    function = tree.body[0]
    if function.name != "add" or [arg.arg for arg in function.args.args] != ["a", "b"]:
        return False, "signature"
    if len(function.body) != 1 or not isinstance(function.body[0], ast.Return):
        return False, "body"
    value = function.body[0].value
    if not isinstance(value, ast.BinOp) or not isinstance(value.left, ast.Name) or not isinstance(value.right, ast.Name):
        return False, "body"
    got = _apply_binop(value.op, value.left.id, value.right.id, 1, 2)
    other = _apply_binop(value.op, value.left.id, value.right.id, -4, 4)
    if got is None or other is None:
        return False, "body"
    if got != 3 or other != 0:
        return False, f"add(1, 2)={got} expected 3"
    return True, "ok"


def _endpoint(base: str) -> str:
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("base_rejected")
    return base.rstrip("/") + "/chat/completions"


def _sum_usage(checks: list[dict[str, Any]]) -> dict[str, Any]:
    if not checks:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "cost_micro_usd": None, "cost_status": "unknown"}
    prompt = 0
    completion = 0
    known = True
    cost = 0
    cost_known = True
    for check in checks:
        if not isinstance(check.get("prompt_tokens"), int) or not isinstance(check.get("completion_tokens"), int):
            known = False
        else:
            prompt += check["prompt_tokens"]
            completion += check["completion_tokens"]
        if check.get("cost_status") != "known" or not isinstance(check.get("cost_micro_usd"), int):
            cost_known = False
        else:
            cost += check["cost_micro_usd"]
    return {
        "prompt_tokens": prompt if known else None,
        "completion_tokens": completion if known else None,
        "total_tokens": prompt + completion if known else None,
        "cost_micro_usd": cost if cost_known and checks else None,
        "cost_status": "known" if cost_known and checks else "unknown",
    }


def _source_of(name: str | None, arguments: dict[str, Any] | None) -> str | None:
    if name != CODING_TOOL or arguments is None:
        return None
    source = arguments.get("source")
    return source if isinstance(source, str) else None


def _overall(checks: list[dict[str, Any]], error: str | None) -> str:
    required = (
        "protocol.tools",
        "protocol.schema",
        "protocol.usage",
        "research.citation",
        "coding.independent_test",
        "coding.recovery",
        "hybrid_retrieval",
        "linux_isolation",
    )
    by_id = {row["id"]: row["status"] for row in checks}
    if error is not None or any(by_id.get(item) == "fail" for item in required):
        return "failed"
    if any(by_id.get(item) != "pass" for item in required):
        return "incomplete"
    return "probed"


def run_live_probe(
    *,
    timeout: float = 30.0,
    retrieve: Any = None,
    isolate: Any = None,
) -> dict[str, Any]:
    missing = [name for name in required_environment() if not os.environ.get(name)]
    if missing:
        return _blocked(missing)
    key = os.environ[LIVE_KEY]
    alias = os.environ[LIVE_ALIAS]
    try:
        url = _endpoint(os.environ[LIVE_BASE].rstrip("/"))
    except RuntimeError as exc:
        report = _blocked([])
        report["status"] = "failed"
        report["error"] = redact_secret(str(exc), key)
        return _redact_tree(report, key)
    hybrid = (retrieve or _hybrid_context)()
    isolation = (isolate or _linux_isolation)(key)
    checks: list[dict[str, Any]] = [
        _check("hybrid_retrieval", hybrid["status"], None, None, hybrid.get("reason")),
        _check("linux_isolation", isolation["status"], None, None, isolation.get("reason")),
    ]
    call_usage: list[dict[str, Any]] = []
    calls_detail: list[dict[str, Any]] = []
    calls = 0
    spent_tokens = 0

    def allow() -> bool:
        return calls < MAX_CALLS and spent_tokens < MAX_TOTAL_TOKENS

    def spend(usage: dict[str, Any]) -> None:
        nonlocal spent_tokens
        call_usage.append(usage)
        total = usage.get("total_tokens")
        if isinstance(total, int):
            spent_tokens += total

    research_status = NOT_RUN
    coding_status = NOT_RUN
    error: str | None = None
    try:
        if not allow():
            raise RuntimeError("call_cap")
        calls += 1
        if hybrid["status"] == "pass":
            body = _research_body(alias, hybrid["passages"])
        else:
            body = _protocol_body(alias)
        status, payload, latency = _post(url, body, key, timeout)
        usage = _usage(payload)
        spend(usage)
        name, arguments = _tool_call(payload)
        tools_ok = status == 200 and name == RESEARCH_TOOL and arguments is not None
        if hybrid["status"] == "pass":
            gold = next(item for item in hybrid["passages"] if item["id"] == hybrid["gold_id"])
            schema_ok = (
                tools_ok
                and isinstance(arguments.get("claim"), str)
                and isinstance(arguments.get("citation_id"), str)
                and isinstance(arguments.get("start_offset"), int)
                and isinstance(arguments.get("end_offset"), int)
            )
            citation_ok = (
                schema_ok
                and arguments["citation_id"] == hybrid["gold_id"]
                and arguments["start_offset"] == gold["start_offset"]
                and arguments["end_offset"] == gold["end_offset"]
                and CLAIM_MARK in arguments["claim"]
            )
            research_status = "pass" if citation_ok else "fail"
            citation_reason = None if citation_ok else "citation_mismatch"
        else:
            schema_ok = tools_ok and isinstance(arguments.get("claim"), str) and isinstance(arguments.get("citation_id"), str)
            citation_ok = False
            research_status = NOT_RUN
            citation_reason = "hybrid_not_run"
        usage_ok = usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None
        research_id = f"call-{calls}"
        calls_detail.append(_call_row(research_id, "research", latency, usage, None))
        checks.append(_check("protocol.tools", "pass" if tools_ok else "fail", latency, usage, None if tools_ok else "tool_call_missing", call_id=research_id))
        checks.append(_check("protocol.schema", "pass" if schema_ok else "fail", latency, usage, None if schema_ok else "schema_mismatch", call_id=research_id))
        checks.append(_check("protocol.usage", "pass" if usage_ok else "fail", latency, usage, None if usage_ok else "usage_missing", call_id=research_id))
        checks.append(_check("research.citation", "pass" if citation_ok else research_status, latency, usage, citation_reason, call_id=research_id))
        if allow():
            calls += 1
            _status, payload, latency = _post(url, _coding_body(alias, repair=None), key, timeout)
            usage = _usage(payload)
            spend(usage)
            name, arguments = _tool_call(payload)
            source = _source_of(name, arguments)
            _first_ok, first_reason = _add_source_ok(source) if isinstance(source, str) else (False, "tool_call_missing")
            rejected = _sha(source)
            first_id = f"call-{calls}"
            calls_detail.append(_call_row(first_id, "coding_first", latency, usage, source))
            checks.append(_check("coding.first_attempt", "pass" if _first_ok else "fail", latency, usage, None if _first_ok else first_reason, call_id=first_id))
            repair_reason = first_reason if first_reason != "ok" else "probe_requires_repair"
            if not allow():
                checks.append(_check("coding.independent_test", NOT_RUN, None, None, "cap"))
                checks.append(_check("coding.recovery", NOT_RUN, None, None, "cap"))
            else:
                calls += 1
                _status, payload, latency = _post(url, _coding_body(alias, repair=repair_reason), key, timeout)
                usage = _usage(payload)
                spend(usage)
                name, arguments = _tool_call(payload)
                repaired_source = _source_of(name, arguments)
                repaired, repair_grade = _add_source_ok(repaired_source) if isinstance(repaired_source, str) else (False, "tool_call_missing")
                accepted = _sha(repaired_source)
                repair_id = f"call-{calls}"
                calls_detail.append(_call_row(repair_id, "coding_repair", latency, usage, repaired_source))
                recovery_ok = repaired and accepted is not None and accepted != rejected
                recovery_reason = None if recovery_ok else "reused_or_failed"
                checks.append(_check("coding.independent_test", "pass" if repaired else "fail", latency, usage, None if repaired else repair_grade, call_id=repair_id))
                checks.append(_check("coding.recovery", "pass" if recovery_ok else "fail", latency, usage, recovery_reason, call_id=repair_id))
                coding_status = "pass" if recovery_ok else "fail"
        else:
            checks.append(_check("coding.independent_test", NOT_RUN, None, None, "cap"))
            checks.append(_check("coding.recovery", NOT_RUN, None, None, "cap"))
    except RuntimeError as exc:
        error = redact_secret(str(exc), key)
        if not any(row["id"] == "protocol.tools" for row in checks):
            checks.append(_check("protocol.tools", "fail", None, None, error))
    report = {
        "status": _overall(checks, error),
        "live_ready": False,
        "missing": [],
        "model_alias": alias,
        "research": research_status,
        "coding": coding_status,
        "hybrid_retrieval": hybrid["status"],
        "linux_isolation": isolation["status"],
        "scripted_substitute": False,
        "calls": calls,
        "calls_detail": calls_detail,
        "caps": _caps(),
        "usage": _sum_usage(call_usage),
        "hybrid": {
            "query": hybrid.get("query"),
            "embedding_revision": hybrid.get("embedding_revision"),
            "gold_id": hybrid.get("gold_id"),
            "bm25": hybrid.get("bm25"),
            "dense": hybrid.get("dense"),
            "rrf": hybrid.get("rrf"),
            "reason": hybrid.get("reason"),
        },
        "linux": {
            "image": isolation.get("image"),
            "flags": isolation.get("flags"),
            "exit_code": isolation.get("exit_code"),
            "network_errno": isolation.get("network_errno"),
            "secret_present": isolation.get("secret_present"),
            "reason": isolation.get("reason"),
        },
        "checks": checks,
        "error": error,
    }
    return _redact_tree(report, key)
