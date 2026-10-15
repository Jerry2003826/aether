"""Live model probe. Missing configuration is blocked, never a scripted pass.

The probe makes at most three short chat-completions calls. It does not run
hybrid retrieval or a Linux runner; those stay not_run.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

LIVE_KEY = "AETHER_LIVE_MODEL_KEY"
LIVE_BASE = "AETHER_LIVE_MODEL_BASE"
LIVE_ALIAS = "AETHER_LIVE_MODEL_ALIAS"
NOT_RUN = "not_run"
MAX_CALLS = 3
MAX_OUTPUT_TOKENS = 128
MAX_TOTAL_TOKENS = 2000
RESEARCH_TOOL = "cite_finding"
CODING_TOOL = "submit_function"
SOURCE_ID = "SRC-1"
PASSAGE = "来源 SRC-1：标准大气压下水的沸点是100摄氏度。"


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
        "caps": {"max_calls": MAX_CALLS, "max_output_tokens": MAX_OUTPUT_TOKENS, "max_total_tokens": MAX_TOTAL_TOKENS},
    }


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


def _research_body(model: str) -> dict[str, Any]:
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
        "messages": [
            {
                "role": "user",
                "content": f"{PASSAGE} 只根据来源调用 {RESEARCH_TOOL}。citation_id 填来源编号，claim 只写来源里的事实。",
            }
        ],
    }


def _coding_body(model: str, *, failure: str | None) -> dict[str, Any]:
    repair = f" 上次失败：{failure}。不要复用上次的源码。" if failure else ""
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "tool_choice": {"type": "function", "function": {"name": CODING_TOOL}},
        "tools": [
            _tool_spec(CODING_TOOL, {"source": {"type": "string"}}, ["source"]),
        ],
        "messages": [
            {
                "role": "user",
                "content": (
                    f"只调用 {CODING_TOOL}。source 是 Python 函数 def add(a, b):，函数体只有 return a + b。不要 import。"
                    + repair
                ),
            }
        ],
    }


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


def _check(check_id: str, status: str, latency_ms: int | None, usage: dict[str, Any] | None, reason: str | None) -> dict[str, Any]:
    row = {"id": check_id, "status": status, "latency_ms": latency_ms, "reason": reason}
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


def _add_source_ok(source: str) -> tuple[bool, str]:
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
    if not isinstance(value, ast.BinOp) or not isinstance(value.op, ast.Add):
        return False, "body"
    names = [side.id for side in (value.left, value.right) if isinstance(side, ast.Name)]
    if names not in (["a", "b"], ["b", "a"]):
        return False, "body"
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


def run_live_probe(*, timeout: float = 30.0) -> dict[str, Any]:
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
    checks: list[dict[str, Any]] = []
    call_usage: list[dict[str, Any]] = []
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
        status, payload, latency = _post(url, _research_body(alias), key, timeout)
        usage = _usage(payload)
        spend(usage)
        name, arguments = _tool_call(payload)
        tools_ok = status == 200 and name == RESEARCH_TOOL and arguments is not None
        schema_ok = tools_ok and isinstance(arguments.get("claim"), str) and isinstance(arguments.get("citation_id"), str)
        usage_ok = usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None
        citation_ok = schema_ok and arguments["citation_id"] == SOURCE_ID and "100" in arguments["claim"]
        checks.append(_check("protocol.tools", "pass" if tools_ok else "fail", latency, usage, None if tools_ok else "tool_call_missing"))
        checks.append(_check("protocol.schema", "pass" if schema_ok else "fail", latency, usage, None if schema_ok else "schema_mismatch"))
        checks.append(_check("protocol.usage", "pass" if usage_ok else "fail", latency, usage, None if usage_ok else "usage_missing"))
        checks.append(_check("research.citation", "pass" if citation_ok else "fail", latency, usage, None if citation_ok else "citation_mismatch"))
        research_status = "pass" if citation_ok else "fail"
        if allow():
            calls += 1
            status, payload, latency = _post(url, _coding_body(alias, failure=None), key, timeout)
            usage = _usage(payload)
            spend(usage)
            name, arguments = _tool_call(payload)
            source = arguments.get("source") if name == CODING_TOOL and arguments else None
            ok, reason = _add_source_ok(source) if isinstance(source, str) else (False, "tool_call_missing")
            rejected = hashlib.sha256(source.encode()).hexdigest() if isinstance(source, str) else None
            if not ok and allow():
                calls += 1
                status, payload, latency = _post(url, _coding_body(alias, failure=reason), key, timeout)
                usage = _usage(payload)
                spend(usage)
                name, arguments = _tool_call(payload)
                source = arguments.get("source") if name == CODING_TOOL and arguments else None
                repaired, repair_reason = _add_source_ok(source) if isinstance(source, str) else (False, "tool_call_missing")
                accepted = hashlib.sha256(source.encode()).hexdigest() if isinstance(source, str) else None
                recovery_ok = repaired and accepted is not None and accepted != rejected
                checks.append(_check("coding.independent_test", "pass" if recovery_ok else "fail", latency, usage, None if recovery_ok else repair_reason))
                checks.append(
                    _check(
                        "coding.recovery",
                        "pass" if recovery_ok else "fail",
                        latency,
                        usage,
                        None if recovery_ok else "reused_or_failed",
                    )
                )
                coding_status = "pass" if recovery_ok else "fail"
            else:
                checks.append(_check("coding.independent_test", "pass" if ok else "fail", latency, usage, None if ok else reason))
                checks.append(_check("coding.recovery", NOT_RUN, None, None, "first_attempt_passed" if ok else "cap"))
                coding_status = "pass" if ok else "fail"
        else:
            checks.append(_check("coding.independent_test", NOT_RUN, None, None, "cap"))
            coding_status = NOT_RUN
    except RuntimeError as exc:
        error = redact_secret(str(exc), key)
        if research_status == NOT_RUN:
            checks.append(_check("protocol.tools", "fail", None, None, error))
    required = ("protocol.tools", "protocol.schema", "protocol.usage", "research.citation", "coding.independent_test")
    by_id = {row["id"]: row["status"] for row in checks}
    probed = all(by_id.get(item) == "pass" for item in required) and error is None
    report = {
        "status": "probed" if probed else "failed",
        "live_ready": False,
        "missing": [],
        "model_alias": alias,
        "research": research_status,
        "coding": coding_status,
        "hybrid_retrieval": NOT_RUN,
        "linux_isolation": NOT_RUN,
        "scripted_substitute": False,
        "calls": calls,
        "caps": {"max_calls": MAX_CALLS, "max_output_tokens": MAX_OUTPUT_TOKENS, "max_total_tokens": MAX_TOTAL_TOKENS},
        "usage": _sum_usage(call_usage),
        "checks": checks,
        "error": error,
    }
    return _redact_tree(report, key)
