"""Full F26 supervisor harness. Missing key stays blocked. live_ready stays false.

Coding copies an authorized snapshot into a fresh directory, asks the OpenHands
SDK for a patch, and grades that directory in a new read-only container. A
failed tree is not graded again. Research encodes bilingual notes with the
live encoder, ranks them against an unrelated note, and asks for a comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from aether_worker.live_harness import (
    LIVE_ALIAS,
    LIVE_BASE,
    LIVE_KEY,
    NOT_RUN,
    _NoRedirect,
    _cosine,
    _endpoint,
    _linux_isolation,
    _protocol_body,
    _tool_call,
    _usage,
    redact_secret,
)
from aether_worker.retrieval import (
    DIMENSION,
    EMBEDDING_REVISION,
    RetrievalError,
    _index_bm25,
    _lexical_ids,
    _measured,
    _sha,
    encode_texts,
    rrf,
)
from aether_worker.tokenize import TOKENIZER_VERSION

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "evals" / "f26_snapshot"
CHECKER = ROOT / "evals" / "f26_checker" / "check.py"
SOURCES = ROOT / "evals" / "f26_sources"
BILLING_NAME = "billing.py"
CHECK_NAME = "check.py"
OPENHANDS_PYTHON = ROOT / "environments" / "openhands" / ".venv" / "bin" / "python"
OPENHANDS_JOB = ROOT / "environments" / "openhands" / "f26_job.py"
MAX_CALLS = 8
MAX_TOTAL_TOKENS = 20000
MAX_OUTPUT_TOKENS = 512
# A cited comparison needs both full sha256s. 512 cut a live report off. ADR-F28.
RESEARCH_OUTPUT_TOKENS = 2048
RESULT_NAME = "f26_acceptance_result.json"
QUERY = "比较中英实验笔记：标准大气压下水的沸点、容器。Compare water boiling point and beaker."
GOLD = ("src-zh", "src-en")
DISTRACTOR = "src-piano"
SOURCE_FILES = (
    ("src-zh", "boiling-zh.txt"),
    ("src-en", "boiling-en.txt"),
    (DISTRACTOR, "piano-zh.txt"),
)
VERIFY_FLAGS = ("--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "32")
VERIFY_IMAGE = "python:3.12-alpine"
_FORWARD_KEYS = {"model", "messages", "max_tokens", "temperature", "tools", "tool_choice"}


def _caps() -> dict[str, int]:
    return {
        "max_calls": MAX_CALLS,
        "max_total_tokens": MAX_TOTAL_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "research_output_tokens": RESEARCH_OUTPUT_TOKENS,
        "spend_cap_usd": 1,
    }


def _tree_sha(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _copy_snapshot(destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(SNAPSHOT, destination)
    shutil.copyfile(CHECKER, destination / CHECK_NAME)


def _reference_sha() -> str:
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary) / "reference"
        _copy_snapshot(destination)
        return _tree_sha(destination)


def _head() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _default_verify(tree: Path) -> dict[str, Any]:
    if shutil.which("docker") is None:
        result = _local_check(tree)
        result["reason"] = "docker_missing"
        return result
    return _docker_check(tree, os.environ.get(LIVE_KEY, ""))


def _local_check(tree: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, CHECK_NAME],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    stdout = (completed.stdout or "") + (completed.stderr or "")
    return {
        "exit_code": completed.returncode,
        "stdout": stdout[-1000:],
        "passed": completed.returncode == 0 and "PASS" in completed.stdout,
        "tree_sha256": _tree_sha(tree),
        "runner": "host",
    }


def _docker_check(tree: Path, secret: str) -> dict[str, Any]:
    command = [
        "docker",
        "run",
        "--rm",
        *VERIFY_FLAGS,
        "-v",
        f"{tree.resolve()}:/work:ro",
        "-w",
        "/work",
        VERIFY_IMAGE,
        "python",
        CHECK_NAME,
    ]
    if secret and any(secret in part for part in command):
        return {
            "exit_code": None,
            "stdout": "",
            "passed": False,
            "tree_sha256": _tree_sha(tree),
            "runner": "docker",
            "reason": "secret_in_command",
        }
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "exit_code": None,
            "stdout": "",
            "passed": False,
            "tree_sha256": _tree_sha(tree),
            "runner": "docker",
            "image": VERIFY_IMAGE,
            "flags": list(VERIFY_FLAGS),
            "reason": type(exc).__name__,
        }
    stdout = redact_secret((completed.stdout or "") + (completed.stderr or ""), secret)
    reason = None
    if completed.returncode != 0 and ("Unable to find image" in stdout or "pull access denied" in stdout):
        reason = "image_unavailable"
    return {
        "exit_code": completed.returncode,
        "stdout": stdout[-1000:],
        "passed": completed.returncode == 0 and "PASS" in (completed.stdout or ""),
        "tree_sha256": _tree_sha(tree),
        "runner": "docker",
        "image": VERIFY_IMAGE,
        "flags": list(VERIFY_FLAGS),
        "reason": reason,
    }


def _read_sources() -> list[dict[str, Any]]:
    rows = []
    for source_id, filename in SOURCE_FILES:
        path = SOURCES / filename
        text = path.read_text(encoding="utf-8")
        rows.append(
            {
                "source_id": source_id,
                "path": f"evals/f26_sources/{filename}",
                "version": 1,
                "text": text,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "start_offset": 0,
                "end_offset": len(text),
            }
        )
    return rows


def _encoder_mode(texts: list[str]) -> str:
    known = _measured()["vectors"]
    if any(_sha(text) not in known for text in texts):
        return "live"
    return "fixture"


def _public_source(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in ("source_id", "path", "version", "content_sha256", "start_offset", "end_offset")}


def _hybrid(sources: list[dict[str, Any]], encode: Callable[[list[str]], list[list[float]]] | None) -> dict[str, Any]:
    texts = [item["text"] for item in sources]
    mode = "injected" if encode is not None else _encoder_mode([QUERY, *texts])
    if mode == "fixture":
        return {"status": NOT_RUN, "reason": "fixture_vectors_not_live", "encoder": "fixture", "sources": [_public_source(item) for item in sources]}
    try:
        vectors = (encode or encode_texts)([QUERY, *texts])
    except RetrievalError as exc:
        return {"status": NOT_RUN, "reason": str(exc), "encoder": mode, "sources": [_public_source(item) for item in sources]}
    if len(vectors) != len(texts) + 1 or any(len(vector) != len(vectors[0]) for vector in vectors):
        return {"status": NOT_RUN, "reason": "dimension_mismatch", "encoder": mode}
    if mode == "live" and len(vectors[0]) != DIMENSION:
        return {"status": NOT_RUN, "reason": "dimension_mismatch", "encoder": mode}
    query_vector, doc_vectors = vectors[0], vectors[1:]
    paired = list(zip(sources, doc_vectors, strict=True))
    dense_ids = [item["source_id"] for item, vector in sorted(paired, key=lambda pair: (-_cosine(query_vector, pair[1]), pair[0]["source_id"]))]
    with tempfile.TemporaryDirectory() as temporary:
        index = Path(temporary) / "bm25"
        _index_bm25(texts, index)
        bm25_ids = _lexical_ids(index, [item["source_id"] for item in sources], QUERY)
    fused = rrf([bm25_ids, dense_ids])
    ranks = {source_id: index + 1 for index, source_id in enumerate(fused)}
    gold_first = DISTRACTOR in ranks and all(source_id in ranks and ranks[source_id] < ranks[DISTRACTOR] for source_id in GOLD)
    covered = set(GOLD) <= set(bm25_ids) and set(GOLD) <= set(dense_ids) and gold_first
    return {
        "status": "pass" if covered else "fail",
        "reason": None if covered else "sources_not_ranked_above_distractor",
        "encoder": mode,
        "embedding_revision": EMBEDDING_REVISION if mode == "live" else None,
        "tokenizer_version": TOKENIZER_VERSION,
        "dimension": len(vectors[0]),
        "dense_index": "exact_cosine",
        "lexical_index": "bm25s",
        "reranker": "not_used",
        "query": QUERY,
        "bm25": bm25_ids,
        "dense": dense_ids,
        "rrf": fused,
        "ranks": ranks,
        "sources": [_public_source(item) for item in sources],
    }


def _coding_prompt(stdout: str, billing: str) -> str:
    return (
        "授权快照里的独立测试失败了。请只根据测试输出修复 billing.py。"
        "不要假设运算符，以测试输出为准。只返回一个 JSON 对象，键 billing_py 的值是完整的新文件。\n"
        f"测试输出：\n{stdout}\n当前 billing.py：\n{billing}"
    )


def _research_prompt(sources: list[dict[str, Any]], ranked: list[str]) -> str:
    by_id = {item["source_id"]: item for item in sources}
    lines = []
    for source_id in ranked:
        item = by_id[source_id]
        lines.append(
            f"{item['source_id']} version {item['version']} sha256 {item['content_sha256']} "
            f"offset {item['start_offset']}-{item['end_offset']}: {item['text']}"
        )
    return (
        "只根据下列检索结果，用中文比较 src-zh 和 src-en：两点相同之处和不同之处。"
        "两条都要写出 source_id、完整 sha256 和 offset。"
        "每条来源的版本单独写成 version=N，N 必须是该来源自己的版本号。"
        "与沸点无关的条目不要写成证据。"
        "笔记没写的不要补。\n" + "\n".join(lines)
    )


_VERSION_FIELD = re.compile(
    r"(?:(?<![A-Za-z])version\s*[=:：]?\s*(\d+)"
    r"|(?<![A-Za-z])version\s+is\s+(\d+)"
    r"|(?<![A-Za-z])version\s*为\s*(\d+)"
    r"|版本号\s*(?:[=:：]|是|为)?\s*(\d+)"
    r"|版本\s*(?:[=:：]|是|为)?\s*(\d+))",
    re.IGNORECASE,
)
_OFFSET_FIELD = re.compile(r"(?<![A-Za-z])offset\s*[=:：]?\s*(\d+)\s*[-–—]\s*(\d+)", re.IGNORECASE)
_SOURCE_ID = re.compile(r"(?<![A-Za-z0-9])src-(?:zh|en|piano)(?![A-Za-z0-9])")
_CLAUSES = re.compile(r"[^\n。；;]+")


def _version_number(match: re.Match[str]) -> int:
    for group in match.groups():
        if group is not None:
            return int(group)
    raise RuntimeError("version_field_without_number")


def _bound_versions(report: str) -> dict[str, set[int]]:
    """Version numbers attached to each source id in the same clause.

    One number in a clause applies to every source named there. Several numbers
    bind to the nearest source. A version mentioned on its own does not count.
    """
    bound: dict[str, set[int]] = {}
    for clause in _CLAUSES.findall(report):
        ids = [(item.group(0), item.start()) for item in _SOURCE_ID.finditer(clause)]
        versions = [(_version_number(item), item.start()) for item in _VERSION_FIELD.finditer(clause)]
        if not ids or not versions:
            continue
        distinct = {number for number, _position in versions}
        if len(distinct) == 1:
            only = distinct.pop()
            for source_id, _position in ids:
                bound.setdefault(source_id, set()).add(only)
            continue
        for number, position in versions:
            source_id = min(ids, key=lambda item: abs(item[1] - position))[0]
            bound.setdefault(source_id, set()).add(number)
    return bound


def _report_cites(report: str, sources: list[dict[str, Any]]) -> bool:
    """Both lab notes must show their id, version, full sha256, and offset.

    `version=1`, `version 1`, `version is 1`, `version 为 1`, `版本 1` and
    `版本号：1` are the same field. The number has to sit with that source.
    A different number, or a version that is not tied to the source, still fails.
    """
    boiling = [item for item in sources if item["source_id"] in GOLD]
    if len(boiling) != 2 or "100" not in report:
        return False
    bound = _bound_versions(report)
    offsets = {(int(match.group(1)), int(match.group(2))) for match in _OFFSET_FIELD.finditer(report)}
    for item in boiling:
        if item["content_sha256"] not in report or item["source_id"] not in report:
            return False
        if bound.get(item["source_id"]) != {item["version"]}:
            return False
        if (item["start_offset"], item["end_offset"]) not in offsets:
            return False
    return True


def _research_verdict(citations: bool, finish_reason: str | None) -> tuple[str, str | None]:
    """A length finish is an incomplete report even when the citations already match."""
    if finish_reason == "length":
        return "fail", "output_truncated"
    if citations:
        return "pass", None
    return "fail", "citation_mismatch"


def _research_finish(rows: list[dict[str, Any]]) -> str | None:
    reasons = [row.get("finish_reason") for row in rows]
    if "length" in reasons:
        return "length"
    for reason in reversed(reasons):
        if isinstance(reason, str) and reason:
            return reason
    return None


def _payload_finish_reason(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    if not isinstance(reason, str):
        return None
    text = reason.strip().lower()
    return text or None


def _grade_protocol(payload: dict[str, Any]) -> dict[str, Any]:
    name, arguments = _tool_call(payload)
    usage = _usage(payload)
    tools_ok = name == "cite_finding" and arguments is not None
    schema_ok = tools_ok and isinstance(arguments.get("claim"), str) and isinstance(arguments.get("citation_id"), str)
    usage_ok = usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None
    status = "pass" if tools_ok and schema_ok and usage_ok else "fail"
    reason = None
    if not tools_ok:
        reason = "tool_call_missing"
    elif not schema_ok:
        reason = "schema_mismatch"
    elif not usage_ok:
        reason = "usage_missing"
    return {"status": status, "tools": "pass" if tools_ok else "fail", "schema": "pass" if schema_ok else "fail", "usage": "pass" if usage_ok else "fail", "reason": reason, "usage_detail": usage}


def _forward_body(raw: bytes, alias: str) -> tuple[bytes, list[str]]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("request_not_object")
    model = payload.get("model")
    if model not in {alias, f"openai/{alias}"}:
        raise RuntimeError("model_rejected")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError("messages_rejected")
    dropped = sorted(set(payload) - _FORWARD_KEYS - {"max_completion_tokens"})
    requested = payload.get("max_tokens", payload.get("max_completion_tokens", MAX_OUTPUT_TOKENS))
    if isinstance(requested, int) and not isinstance(requested, bool) and 1 <= requested <= RESEARCH_OUTPUT_TOKENS:
        max_tokens = requested
    elif isinstance(requested, int) and not isinstance(requested, bool) and requested > RESEARCH_OUTPUT_TOKENS:
        max_tokens = RESEARCH_OUTPUT_TOKENS
    else:
        max_tokens = MAX_OUTPUT_TOKENS
    forwarded: dict[str, Any] = {"model": alias, "messages": messages, "max_tokens": max_tokens}
    for key in ("temperature", "tools", "tool_choice"):
        if key in payload:
            forwarded[key] = payload[key]
    return json.dumps(forwarded).encode(), dropped


class _Gateway(ThreadingHTTPServer):
    def __init__(self, upstream: str, key: str, alias: str) -> None:
        self.upstream = upstream
        self.key = key
        self.alias = alias
        self.calls: list[dict[str, Any]] = []
        self.total_tokens = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _GatewayHandler)

    def allow(self) -> bool:
        with self._lock:
            return len(self.calls) < MAX_CALLS and self.total_tokens < MAX_TOTAL_TOKENS

    def record(self, row: dict[str, Any], counted: int) -> None:
        with self._lock:
            self.calls.append(row)
            self.total_tokens += counted


class _GatewayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server: _Gateway = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        started = time.perf_counter()
        if self.headers.get("Authorization") != f"Bearer {os.environ.get('AETHER_GATEWAY_TOKEN', '')}":
            self._send(401, {"error": "unauthorized"})
            return
        if not server.allow():
            server.record(_call_row(server, 429, None, None, [], started), 0)
            self._send(429, {"error": "cap"})
            return
        try:
            forwarded, dropped = _forward_body(raw, server.alias)
        except (RuntimeError, json.JSONDecodeError) as exc:
            server.record(_call_row(server, 400, None, raw, [], started, reason=type(exc).__name__), 0)
            self._send(400, {"error": type(exc).__name__})
            return
        request = urllib.request.Request(
            server.upstream,
            data=forwarded,
            headers={"Authorization": f"Bearer {server.key}", "Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=60) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = redact_secret(exc.read().decode("utf-8", errors="replace")[:500], server.key)
            server.record(_call_row(server, exc.code, None, forwarded, dropped, started, reason="upstream_http"), 0)
            self._send(exc.code, {"error": detail})
            return
        except Exception as exc:
            server.record(_call_row(server, 502, None, forwarded, dropped, started, reason=type(exc).__name__), 0)
            self._send(502, {"error": type(exc).__name__})
            return
        if server.key and server.key.encode() in body:
            body = body.replace(server.key.encode(), b"[REDACTED]")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            server.record(_call_row(server, 502, None, forwarded, dropped, started, reason="response_not_json"), MAX_OUTPUT_TOKENS)
            self._send(502, {"error": "response_not_json"})
            return
        if not isinstance(payload, dict):
            server.record(_call_row(server, 502, None, forwarded, dropped, started, reason="response_not_object"), MAX_OUTPUT_TOKENS)
            self._send(502, {"error": "response_not_object"})
            return
        usage = _usage(payload)
        counted = usage["total_tokens"] if isinstance(usage["total_tokens"], int) else MAX_OUTPUT_TOKENS
        server.record(_call_row(server, status, payload, forwarded, dropped, started), counted)
        self._send(status, payload)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _call_row(
    server: _Gateway,
    status: int,
    payload: dict[str, Any] | None,
    forwarded: bytes | None,
    dropped: list[str],
    started: float,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    usage = _usage(payload or {})
    content = ""
    upstream_id = None
    if isinstance(payload, dict):
        upstream_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        try:
            content = str(payload["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError):
            content = ""
    return {
        "id": f"call-{len(server.calls) + 1}",
        "upstream_id": upstream_id,
        "http_status": status,
        "model": server.alias,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "request_sha256": hashlib.sha256(forwarded).hexdigest() if forwarded is not None else None,
        "completion_sha256": hashlib.sha256(content.encode()).hexdigest() if content else None,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_micro_usd": usage["cost_micro_usd"],
        "cost_status": usage["cost_status"],
        "dropped_fields": dropped,
        "reason": reason,
        "finish_reason": _payload_finish_reason(payload),
        "source": "gateway",
    }


def _blocked(missing: list[str]) -> dict[str, Any]:
    return {
        "status": "blocked",
        "live_ready": False,
        "missing": missing,
        "scripted_substitute": False,
        "caps": _caps(),
        "calls": 0,
        "calls_detail": [],
        "coding": {"status": NOT_RUN},
        "research": {"status": NOT_RUN, "semantic_support": "not_judged"},
        "protocol": {"status": NOT_RUN},
        "linux_isolation": {"status": NOT_RUN},
        "cost_status": "unknown",
    }


def _job_env(gateway: str, token: str, secret: str, *, output_tokens: int = MAX_OUTPUT_TOKENS) -> dict[str, str]:
    env = os.environ.copy()
    env.pop(LIVE_KEY, None)
    if secret:
        for name, value in list(env.items()):
            if secret in value:
                env.pop(name, None)
    env["AETHER_JOB_TOKEN"] = token
    env["AETHER_GATEWAY_URL"] = gateway
    env["AETHER_GATEWAY_TOKEN"] = token
    env["AETHER_MAX_OUTPUT_TOKENS"] = str(output_tokens)
    env["OPENHANDS_SUPPRESS_BANNER"] = "1"
    env.pop("AETHER_ATTEMPT_DIR", None)
    return env


def _default_launch(prompt: str, env: dict[str, str], secret: str) -> tuple[str, dict[str, Any]]:
    if not OPENHANDS_PYTHON.is_file():
        raise RuntimeError("openhands_venv_missing")
    completed = subprocess.run(
        [str(OPENHANDS_PYTHON), str(OPENHANDS_JOB)],
        input=prompt,
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    stdout = completed.stdout or ""
    if secret and secret in stdout:
        raise RuntimeError("secret_in_job_output")
    lines = [item for item in stdout.splitlines() if item.startswith("{")]
    if completed.returncode != 0 or not lines:
        detail = redact_secret((stdout or completed.stderr or "openhands_failed")[-500:], secret)
        raise RuntimeError(detail)
    payload = json.loads(lines[-1])
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    meta = {"sdk": payload.get("sdk"), "release": payload.get("release"), "wrote_billing": payload.get("wrote_billing")}
    return str(payload.get("content") or ""), meta


def _post_gateway(url: str, body: dict[str, Any], token: str, secret: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_secret(exc.read().decode("utf-8", errors="replace")[:500], secret)
        raise RuntimeError(f"http_{exc.code}:{detail}") from None
    except Exception as exc:
        raise RuntimeError(redact_secret(f"{type(exc).__name__}:{exc}", secret)) from None
    if not isinstance(payload, dict):
        raise RuntimeError("response_not_object")
    return payload


def _mark_new(gateway: _Gateway, before: int, kind: str, prompt: str) -> list[dict[str, Any]]:
    fresh = gateway.calls[before:]
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    for row in fresh:
        row["kind"] = kind
        row["prompt_sha256"] = digest
    return fresh


def _injected_call(kind: str, prompt: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    usage = _usage(payload or {})
    return {
        "id": "",
        "kind": kind,
        "source": "injected",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_micro_usd": usage["cost_micro_usd"],
        "cost_status": usage["cost_status"],
    }


def _number_calls(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows, start=1):
        if row.get("source") == "injected":
            row["id"] = f"call-{index}"


def _docker_blocked(row: dict[str, Any]) -> str | None:
    if row.get("reason") == "docker_missing":
        return "docker_missing"
    if row.get("reason") == "image_unavailable":
        return "image_unavailable"
    return None


def _grade_attempt(
    attempt_dir: Path,
    baseline: dict[str, Any],
    snapshot_sha: str,
    pre_patch: str,
    checker_sha: str,
    verify: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    original = (SNAPSHOT / BILLING_NAME).read_bytes()
    current = (attempt_dir / BILLING_NAME).read_bytes()
    graded = verify(attempt_dir)
    graded["pre_patch_tree_sha256"] = pre_patch
    graded["patch_sha256"] = hashlib.sha256(current).hexdigest()
    graded["patch_writer"] = "openhands" if current != original else "unchanged"
    graded["checker_sha256"] = hashlib.sha256((attempt_dir / CHECK_NAME).read_bytes()).hexdigest()
    checker_ok = graded["checker_sha256"] == checker_sha
    fresh = pre_patch == snapshot_sha
    graded["reused_failed_tree"] = not fresh
    graded["accepted"] = bool(graded.get("passed")) and graded.get("tree_sha256") != baseline.get("tree_sha256") and fresh and checker_ok
    if not checker_ok:
        graded["reason"] = "checker_modified"
    elif not fresh:
        graded["reason"] = "reused_failed_tree"
    return graded


def _finish_coding(coding: dict[str, Any], baseline: dict[str, Any]) -> None:
    accepted = next((item for item in coding["attempts"] if item.get("accepted")), None)
    blocked = _docker_blocked(baseline) or next((_docker_blocked(item) for item in coding["attempts"] if _docker_blocked(item)), None)
    if accepted and accepted.get("runner") == "docker" and accepted.get("exit_code") == 0:
        coding["status"] = "pass"
        coding["reason"] = None
    elif blocked:
        coding["status"] = NOT_RUN
        coding["reason"] = blocked
    else:
        coding["status"] = "fail"


def _research_from_hybrid(hybrid: dict[str, Any]) -> dict[str, Any]:
    ready = hybrid["status"] == "pass"
    research: dict[str, Any] = {
        "status": NOT_RUN if ready else hybrid["status"],
        "reason": "comparison_not_run" if ready else hybrid.get("reason"),
        "semantic_support": "not_judged",
        "version_store": "acceptance_report",
    }
    if ready:
        kept = (
            "encoder",
            "embedding_revision",
            "tokenizer_version",
            "dimension",
            "dense_index",
            "lexical_index",
            "reranker",
            "query",
            "bm25",
            "dense",
            "rrf",
            "ranks",
            "sources",
        )
        research.update({key: hybrid[key] for key in kept})
    elif hybrid.get("sources"):
        research["sources"] = hybrid["sources"]
    return research


def _overall(protocol: dict[str, Any], coding: dict[str, Any], research: dict[str, Any], linux: dict[str, Any], error: str | None) -> str:
    statuses = (protocol["status"], coding["status"], research["status"], linux["status"])
    if error is not None or any(item == "fail" for item in statuses):
        return "failed"
    if any(item == NOT_RUN for item in statuses):
        return "incomplete"
    if all(item == "pass" for item in statuses):
        return "probed"
    return "failed"


def _cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "cost_micro_usd": None, "cost_status": "unknown"}
    prompt = completion = total = cost = 0
    tokens_known = cost_known = True
    for row in rows:
        if not all(isinstance(row.get(key), int) for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
            tokens_known = False
        else:
            prompt += row["prompt_tokens"]
            completion += row["completion_tokens"]
            total += row["total_tokens"]
        if row.get("cost_status") != "known" or not isinstance(row.get("cost_micro_usd"), int):
            cost_known = False
        else:
            cost += row["cost_micro_usd"]
    return {
        "prompt_tokens": prompt if tokens_known else None,
        "completion_tokens": completion if tokens_known else None,
        "total_tokens": total if tokens_known else None,
        "cost_micro_usd": cost if cost_known else None,
        "cost_status": "known" if cost_known else "unknown",
    }


def run_f26_acceptance(
    *,
    encode: Callable[[list[str]], list[list[float]]] | None = None,
    verify: Callable[[Path], dict[str, Any]] | None = None,
    launch: Callable[[str, dict[str, str]], str] | None = None,
    protocol: Callable[[], dict[str, Any]] | None = None,
    isolate: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    scripted = any(item is not None for item in (encode, verify, launch, protocol, isolate))
    missing = [name for name in (LIVE_KEY, LIVE_BASE, LIVE_ALIAS) if not os.environ.get(name)]
    live = launch is None and protocol is None
    if missing and live:
        return _blocked(missing)
    secret = os.environ.get(LIVE_KEY, "")
    alias = os.environ.get(LIVE_ALIAS, "")
    sources = _read_sources()
    hybrid = _hybrid(sources, encode)
    linux = (isolate or _linux_isolation)(secret)
    snapshot_sha = _reference_sha()
    checker_sha = hashlib.sha256(CHECKER.read_bytes()).hexdigest()
    coding: dict[str, Any] = {
        "status": NOT_RUN,
        "snapshot_sha256": snapshot_sha,
        "checker_sha256": checker_sha,
        "executor": "openhands-sdk",
        "attempts": [],
    }
    research = _research_from_hybrid(hybrid)
    calls: list[dict[str, Any]] = []
    protocol_check: dict[str, Any] = {"status": NOT_RUN, "reason": "not_started"}
    observed: dict[str, Any] = {}
    error: str | None = None
    gateway: _Gateway | None = None
    thread: threading.Thread | None = None
    token = secrets.token_hex(16)
    gateway_url = "http://injected/v1"
    work: Path | None = None
    try:
        if live:
            upstream = _endpoint(os.environ[LIVE_BASE].rstrip("/"))
            gateway = _Gateway(upstream, secret, alias)
            os.environ["AETHER_GATEWAY_TOKEN"] = token
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            gateway_url = f"http://127.0.0.1:{gateway.server_address[1]}/v1"
        protocol_body = _protocol_body(alias or "deepseek-chat")
        protocol_prompt = json.dumps(protocol_body, ensure_ascii=False, sort_keys=True)
        try:
            if protocol is not None:
                payload = protocol()
                calls.append(_injected_call("protocol", protocol_prompt, payload))
            elif gateway is not None:
                before = len(gateway.calls)
                payload = _post_gateway(gateway_url, protocol_body, token, secret)
                calls.extend(_mark_new(gateway, before, "protocol", protocol_prompt))
            else:
                payload = {}
            protocol_check = _grade_protocol(payload)
        except Exception as exc:
            protocol_check = {"status": "fail", "reason": redact_secret(f"{type(exc).__name__}:{exc}", secret)}
        grader = verify or _default_verify
        work = Path(tempfile.mkdtemp(prefix="aether-f26-"))
        baseline_dir = work / "baseline"
        _copy_snapshot(baseline_dir)
        baseline = grader(baseline_dir)
        baseline["accepted"] = False
        coding["baseline"] = baseline
        blocked = _docker_blocked(baseline)
        if blocked and verify is None:
            coding["status"] = NOT_RUN
            coding["reason"] = blocked
        else:
            try:
                billing = (SNAPSHOT / BILLING_NAME).read_text(encoding="utf-8")
                prompt = _coding_prompt(str(baseline.get("stdout") or ""), billing)
                for _ in range(2):
                    attempt_dir = work / f"attempt-{len(coding['attempts']) + 1}"
                    _copy_snapshot(attempt_dir)
                    pre_patch = _tree_sha(attempt_dir)
                    env = _job_env(gateway_url, token, secret)
                    env["AETHER_ATTEMPT_DIR"] = str(attempt_dir.resolve())
                    if launch is not None:
                        content = launch(prompt, env)
                        calls.append(_injected_call("coding", prompt))
                    else:
                        if gateway is None:
                            raise RuntimeError("gateway_missing")
                        before = len(gateway.calls)
                        content, meta = _default_launch(prompt, env, secret)
                        observed.update(meta)
                        calls.extend(_mark_new(gateway, before, "coding", prompt))
                    graded = _grade_attempt(attempt_dir, baseline, snapshot_sha, pre_patch, checker_sha, grader)
                    graded["response_sha256"] = hashlib.sha256(content.encode()).hexdigest()
                    coding["attempts"].append(graded)
                    if graded["accepted"]:
                        break
                    billing = (attempt_dir / BILLING_NAME).read_text(encoding="utf-8")
                    prompt = _coding_prompt(str(graded.get("stdout") or graded.get("reason") or ""), billing)
                coding["sdk"] = observed.get("sdk")
                coding["openhands_release"] = observed.get("release")
                _finish_coding(coding, baseline)
            except Exception as exc:
                if str(exc) == "openhands_venv_missing":
                    coding["status"] = NOT_RUN
                    coding["reason"] = "openhands_venv_missing"
                else:
                    coding["status"] = "fail"
                    coding["reason"] = redact_secret(f"{type(exc).__name__}:{exc}", secret)
        if hybrid["status"] == "pass":
            try:
                prompt = _research_prompt(sources, list(hybrid["rrf"]))
                env = _job_env(gateway_url, token, secret, output_tokens=RESEARCH_OUTPUT_TOKENS)
                fresh: list[dict[str, Any]]
                if launch is not None:
                    report_text = launch(prompt, env)
                    fresh = [_injected_call("research", prompt)]
                    calls.extend(fresh)
                else:
                    if gateway is None:
                        raise RuntimeError("gateway_missing")
                    before = len(gateway.calls)
                    report_text, meta = _default_launch(prompt, env, secret)
                    observed.update(meta)
                    fresh = _mark_new(gateway, before, "research", prompt)
                    calls.extend(fresh)
                stored = report_text[:4000]
                citations = _report_cites(stored, sources)
                research["report"] = stored
                research["report_sha256"] = hashlib.sha256(report_text.encode()).hexdigest()
                research["citations_cover_sources"] = citations
                research["status"], research["reason"] = _research_verdict(citations, _research_finish(fresh))
            except Exception as exc:
                research["status"] = NOT_RUN if str(exc) == "openhands_venv_missing" else "fail"
                research["reason"] = "openhands_venv_missing" if str(exc) == "openhands_venv_missing" else redact_secret(f"{type(exc).__name__}:{exc}", secret)
    except Exception as exc:
        if str(exc) == "openhands_venv_missing":
            coding["status"] = NOT_RUN
            coding["reason"] = "openhands_venv_missing"
        else:
            error = redact_secret(f"{type(exc).__name__}:{exc}", secret)
        if gateway is not None and not calls:
            calls = list(gateway.calls)
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
        if thread is not None:
            thread.join(timeout=2)
        os.environ.pop("AETHER_GATEWAY_TOKEN", None)
    _number_calls(calls)
    usage = _cost(calls)
    report = {
        "status": _overall(protocol_check, coding, research, linux, error),
        "live_ready": False,
        "scripted_substitute": scripted,
        "git_commit": _head(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model_alias": alias or None,
        "model_base": os.environ.get(LIVE_BASE),
        "caps": _caps(),
        "calls": len(calls),
        "calls_detail": calls,
        "usage": usage,
        "cost_status": usage["cost_status"],
        "protocol": protocol_check,
        "coding": coding,
        "research": research,
        "linux_isolation": linux,
        "error": error,
    }
    return _redact_report(report, secret)


def _redact_report(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return redact_secret(value, secret)
    if isinstance(value, list):
        return [_redact_report(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact_report(item, secret) for key, item in value.items()}
    return value
