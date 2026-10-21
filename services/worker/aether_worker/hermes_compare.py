"""Capped four-way Hermes/AETHER run. It never declares a winner.

The provider key stays in this supervisor process. Hermes and the AETHER
completion receive only a job token for the local gateway.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from aether_gateway.sse import data_fields, looks_like_event_stream
from aether_worker.hermes_baseline import GROUPS, holdout_status, load_dev_cases, summarize
from aether_worker.live_harness import LIVE_ALIAS, LIVE_BASE, LIVE_KEY, _NoRedirect, _endpoint, _usage, redact_secret

ROOT = Path(__file__).resolve().parents[3]
DEV_CASES = ROOT / "evals" / "dev_cases.jsonl"
HERMES_BIN = ROOT / ".hermes" / ".venv" / "bin" / "hermes"
LOCK_PATH = ROOT / "config" / "hermes.lock.json"
RESULT_NAME = "hermes_compare_result.json"
MAX_CALLS = 8
MAX_TOTAL_TOKENS = 8000
MAX_OUTPUT_TOKENS = 128
WALL_SECONDS = 45
JSON_TYPE = "application/json"
EVENT_STREAM = "text/event-stream"
MODEL_NAME = "deepseek-chat"
EMPTY_TOOLSET = "bot_room"
HOST_TOOLSETS = (
    "terminal",
    "file",
    "web",
    "search",
    "browser",
    "code_execution",
    "computer_use",
    "delegation",
    "skills",
    "cronjob",
    "clarify",
    "todo",
    "vision",
    "image_gen",
    "video",
    "video_gen",
    "tts",
    "debugging",
    "safe",
    "coding",
    "kanban",
    "session_search",
    "connections",
)
_ENV_ALLOW = (
    "PATH",
    "TERM",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)
TASK = "计算 1+2。只回复一个 JSON 对象，键 answer，值是整数。不要其他文字。"
LESSON = "冻结笔记：若题目要求只回复 JSON，则不要加解释或代码块。"
CASE_ID = "DEV-COMPARE-SUM"
UNMATCHED = [
    "Hermes 用钉住的 CLI 单次 chat --oneshot，AETHER 这侧是同一网关的一次 chat completion，不是 Temporal 任务。",
    "两边都不读 holdout。公开开发集只记录哈希，不把 28 条规格说明发给模型。",
    "四组用户题相同。learned 在题目前加上同一段冻结笔记。Hermes learned 还把这段笔记写成 HERMES_HOME/memories/MEMORY.md，并且不传 --ignore-rules，让 CLI 把冻结记忆放进系统提示。AETHER 没有那套记忆目录。",
    "Hermes 子进程只拿允许名单里的环境变量。HOME 和 HERMES_HOME 都指向隔离目录。--toolsets bot_room 在钉住的 v0.22.1 里是空工具集，平台工具列表也是空的，终端和文件工具不进这次调用。",
    "四组共用同一墙钟上限。请求里的 stream 和 stream_options 原样转发；上游返回 SSE 时网关按 SSE 回给 Hermes。",
    "Hermes 还会单独请求一次会话标题，那次是非流式补全，和正式回答一起计入 8 次上限。AETHER 这侧没有这步。",
]


def _caps() -> dict[str, int]:
    return {
        "max_calls": MAX_CALLS,
        "max_total_tokens": MAX_TOTAL_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "wall_seconds": WALL_SECONDS,
        "spend_cap_usd": 1,
    }


def _unchecked_identity() -> dict[str, Any]:
    return {"status": "not_checked", "matches": None}


def prompt_for(learned: bool) -> str:
    if learned:
        return LESSON + "\n" + TASK
    return TASK


def grade_answer(text: str) -> dict[str, Any]:
    answer = None
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("answer"), int) and not isinstance(payload.get("answer"), bool):
        answer = payload["answer"]
    return {
        "success": answer == 3,
        "answer": answer,
        "abstained": answer is None,
        "false_completion": answer is not None and answer != 3,
    }


def _estimate_tokens(payload: dict[str, Any]) -> int:
    messages = payload.get("messages")
    text = json.dumps(messages if isinstance(messages, list) else payload, ensure_ascii=False)
    return max(1, (len(text) + 3) // 4) + MAX_OUTPUT_TOKENS


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
    kept = {"model", "messages", "max_tokens", "temperature", "stream", "stream_options"}
    dropped = sorted(set(payload) - kept)
    requested = payload.get("max_tokens", MAX_OUTPUT_TOKENS)
    max_tokens = MAX_OUTPUT_TOKENS
    if isinstance(requested, int) and not isinstance(requested, bool) and 1 <= requested <= MAX_OUTPUT_TOKENS:
        max_tokens = requested
    forwarded: dict[str, Any] = {"model": alias, "messages": messages, "max_tokens": max_tokens, "temperature": 0}
    if isinstance(payload.get("stream"), bool):
        forwarded["stream"] = payload["stream"]
    if isinstance(payload.get("stream_options"), dict):
        forwarded["stream_options"] = payload["stream_options"]
    return json.dumps(forwarded).encode(), dropped


def _sse_payloads(body: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    # httpx-sse splits the events: CRLF, LF and lone CR line ends, a leading BOM, multi-line data.
    for data in data_fields(body):
        raw = data.strip()
        if raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _sse_content(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for event in events:
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        message = choices[0].get("message")
        piece = delta.get("content") if isinstance(delta, dict) else None
        if piece is None and isinstance(message, dict):
            piece = message.get("content")
        if isinstance(piece, str):
            parts.append(piece)
    return "".join(parts)


def _json_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError):
        return ""
    return str(content or "")


def _meter_body(body: bytes, content_type: str) -> tuple[str, dict[str, Any], str | None, str | None]:
    if EVENT_STREAM in content_type.lower() or looks_like_event_stream(body):
        events = _sse_payloads(body)
        usage = _usage({})
        model = None
        upstream_id = None
        for event in events:
            if isinstance(event.get("usage"), dict):
                usage = _usage(event)
            if isinstance(event.get("model"), str):
                model = event["model"]
            if isinstance(event.get("id"), str):
                upstream_id = event["id"]
        return _sse_content(events), usage, model, upstream_id
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "", _usage({}), None, None
    if not isinstance(payload, dict):
        return "", _usage({}), None, None
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    upstream_id = payload.get("id") if isinstance(payload.get("id"), str) else None
    return _json_content(payload), _usage(payload), model, upstream_id


class _Gateway(ThreadingHTTPServer):
    def __init__(self, upstream: str, key: str, alias: str) -> None:
        self.upstream = upstream
        self.key = key
        self.alias = alias
        self.calls: list[dict[str, Any]] = []
        self.total_tokens = 0
        self._inflight = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def allow(self) -> bool:
        with self._lock:
            return len(self.calls) + self._inflight < MAX_CALLS and self.total_tokens < MAX_TOTAL_TOKENS

    def try_reserve(self, estimate: int) -> bool:
        with self._lock:
            if len(self.calls) + self._inflight >= MAX_CALLS:
                return False
            if self.total_tokens + estimate > MAX_TOTAL_TOKENS:
                return False
            self.total_tokens += estimate
            self._inflight += 1
            return True

    def release(self, estimate: int) -> None:
        with self._lock:
            self.total_tokens -= estimate
            self._inflight -= 1

    def finish(self, estimate: int, row: dict[str, Any]) -> None:
        measured = row.get("total_tokens")
        counted = measured if isinstance(measured, int) else estimate
        row["held_tokens"] = counted
        with self._lock:
            self.total_tokens += counted - estimate
            self._inflight -= 1
            row["id"] = f"call-{len(self.calls) + 1}"
            self.calls.append(row)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server: _Gateway = self.server  # type: ignore[assignment]
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Authorization") != f"Bearer {os.environ.get('AETHER_GATEWAY_TOKEN', '')}":
            self._send(401, {"error": "unauthorized"})
            return
        reservation = self._reservation(server, raw)
        if reservation is None:
            return
        self._proxy(server, *reservation)

    def _reservation(self, server: _Gateway, raw: bytes) -> tuple[int, bytes, list[str]] | None:
        try:
            incoming = json.loads(raw)
        except json.JSONDecodeError:
            self._send(400, {"error": "rejected"})
            return None
        if not isinstance(incoming, dict):
            self._send(400, {"error": "rejected"})
            return None
        estimate = _estimate_tokens(incoming)
        if not server.try_reserve(estimate):
            self._send(429, {"error": "cap"})
            return None
        try:
            forwarded, dropped = _forward_body(raw, server.alias)
        except (RuntimeError, json.JSONDecodeError):
            server.release(estimate)
            self._send(400, {"error": "rejected"})
            return None
        return estimate, forwarded, dropped

    def _proxy(self, server: _Gateway, estimate: int, forwarded: bytes, dropped: list[str]) -> None:
        started = time.perf_counter()
        try:
            status, content_type, body = _fetch_upstream(server, forwarded)
        except (urllib.error.URLError, TimeoutError, OSError):
            self._finish_failure(server, estimate, forwarded, dropped, started, 504)
            self._send(504, {"error": "upstream_unreachable"})
            return
        if server.key and server.key.encode() in body:
            body = body.replace(server.key.encode(), b"[REDACTED]")
        if status >= 400:
            self._finish_failure(server, estimate, forwarded, dropped, started, status)
            detail = redact_secret(body.decode("utf-8", errors="replace")[:300], server.key)
            self._send(status, {"error": detail})
            return
        content, usage, model, upstream_id = _meter_body(body, content_type)
        server.finish(estimate, _call_row(server, status, content, usage, forwarded, dropped, started, model, upstream_id))
        self._send_bytes(status, body, _response_type(forwarded, content_type, body))

    def _finish_failure(self, server: _Gateway, estimate: int, forwarded: bytes, dropped: list[str], started: float, status: int) -> None:
        server.finish(estimate, _call_row(server, status, "", _usage({}), forwarded, dropped, started, None, None))

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        self._send_bytes(status, json.dumps(payload).encode(), JSON_TYPE)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _response_type(forwarded: bytes, content_type: str, body: bytes) -> str:
    if json.loads(forwarded).get("stream") is True and looks_like_event_stream(body):
        return content_type if EVENT_STREAM in content_type.lower() else EVENT_STREAM
    return content_type or JSON_TYPE


def _fetch_upstream(server: _Gateway, forwarded: bytes) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        server.upstream,
        data=forwarded,
        headers={"Authorization": f"Bearer {server.key}", "Content-Type": JSON_TYPE},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=WALL_SECONDS) as response:
            return response.status, response.headers.get("Content-Type", JSON_TYPE), response.read()
    except urllib.error.HTTPError as exc:
        content_type = exc.headers.get("Content-Type", JSON_TYPE) if exc.headers else JSON_TYPE
        return exc.code, content_type, exc.read()


def _call_row(
    server: _Gateway,
    status: int,
    content: str,
    usage: dict[str, Any],
    forwarded: bytes,
    dropped: list[str],
    started: float,
    model: str | None,
    upstream_id: str | None,
) -> dict[str, Any]:
    return {
        "upstream_id": upstream_id,
        "http_status": status,
        "requested_model": server.alias,
        "model": model,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "request_sha256": hashlib.sha256(forwarded).hexdigest(),
        "completion_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_micro_usd": usage["cost_micro_usd"],
        "cost_status": usage["cost_status"],
        "dropped_fields": dropped,
        "source": "gateway",
    }


def _probe_env(home: Path) -> dict[str, str]:
    env = {"HOME": str(home), "HERMES_HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
    path = os.environ.get("PATH")
    secret = os.environ.get(LIVE_KEY, "")
    if path and (not secret or secret not in path):
        env["PATH"] = path
    return env


def _child_env(home: Path, gateway: str, token: str, secret: str) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "HERMES_INFERENCE_MODEL": MODEL_NAME,
        "OPENAI_BASE_URL": gateway,
        "OPENAI_API_KEY": token,
        "CUSTOM_BASE_URL": gateway,
        "CUSTOM_API_KEY": token,
    }
    for name in _ENV_ALLOW:
        value = os.environ.get(name)
        if not value or (secret and secret in value):
            continue
        env[name] = value
    return env


def _config_text(gateway: str) -> str:
    disabled = "\n".join(f"    - {name}" for name in HOST_TOOLSETS)
    return (
        "model:\n"
        "  provider: custom\n"
        f"  default: {MODEL_NAME}\n"
        f"  base_url: {gateway}\n"
        "  key_env: OPENAI_API_KEY\n"
        "  api_mode: chat_completions\n"
        "memory:\n"
        "  memory_enabled: true\n"
        "  user_profile_enabled: false\n"
        "platform_toolsets:\n"
        "  cli: []\n"
        "skills:\n"
        "  auto_load: []\n"
        "agent:\n"
        "  disabled_toolsets:\n"
        f"{disabled}\n"
    )


def _write_home(home: Path, gateway: str, token: str, *, learned: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(_config_text(gateway), encoding="utf-8")
    (home / ".env").write_text(f"CUSTOM_API_KEY={token}\nOPENAI_API_KEY={token}\n", encoding="utf-8")
    if learned:
        note = home / "memories"
        note.mkdir()
        (note / "MEMORY.md").write_text(LESSON + "\n", encoding="utf-8")


def _hermes_argv(binary: Path, prompt: str) -> list[str]:
    return [
        str(binary),
        "chat",
        "-q",
        prompt,
        "--oneshot",
        "--max-turns",
        "1",
        "--quiet",
        "--run-budget",
        str(WALL_SECONDS),
        "--toolsets",
        EMPTY_TOOLSET,
        "-m",
        MODEL_NAME,
        "--provider",
        "custom",
    ]


def _default_hermes(binary: Path, prompt: str, env: dict[str, str], work: Path) -> str:
    completed = subprocess.run(
        _hermes_argv(binary, prompt),
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=WALL_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "hermes_failed")[-400:]
        raise RuntimeError(detail)
    return completed.stdout or ""


def _post_gateway(url: str, body: dict[str, Any], token: str, secret: str) -> str:
    request = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": JSON_TYPE},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=WALL_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_secret(exc.read().decode("utf-8", errors="replace")[:300], secret)
        raise RuntimeError(f"http_{exc.code}:{detail}") from None
    if not isinstance(payload, dict):
        raise RuntimeError("response_not_object")
    try:
        return str(payload["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("response_missing_content") from exc


def _stamp(calls: list[dict[str, Any]], before: int, group: str, prompt: str) -> None:
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    for row in calls[before:]:
        row["group"] = group
        row["prompt_sha256"] = digest


def _keep_metered(gateway: _Gateway, calls: list[dict[str, Any]], before: int, group: str, prompt: str) -> None:
    if len(gateway.calls) <= before:
        return
    _stamp(gateway.calls, before, group, prompt)
    calls.extend(gateway.calls[before:])


def _held_total(rows: list[dict[str, Any]]) -> int | None:
    total = 0
    seen = False
    for row in rows:
        value = row.get("held_tokens")
        if not isinstance(value, int):
            value = row.get("total_tokens")
        if isinstance(value, int):
            total += value
            seen = True
    return total if seen else None


def _append_injected(calls: list[dict[str, Any]], name: str, prompt: str) -> None:
    calls.append(
        {
            "id": "",
            "group": name,
            "source": "injected",
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "cost_status": "unknown",
            "total_tokens": None,
        }
    )


def _invoke_hermes(
    name: str,
    prompt: str,
    *,
    learned: bool,
    home: Path,
    work: Path,
    gateway: _Gateway | None,
    gateway_url: str,
    token: str,
    secret: str,
    binary: Path,
    launch: Callable[[str, dict[str, str], Path], str] | None,
    calls: list[dict[str, Any]],
) -> str:
    child = _child_env(home, gateway_url, token, secret)
    if launch is not None:
        text = launch(prompt, child, home)
        _append_injected(calls, name, prompt)
        return text
    if gateway is None:
        raise RuntimeError("gateway_missing")
    _write_home(home, gateway_url, token, learned=learned)
    work.mkdir(parents=True, exist_ok=True)
    before = len(gateway.calls)
    try:
        text = _default_hermes(binary, prompt, child, work)
        if len(gateway.calls) == before:
            raise RuntimeError("no_metered_call")
        return text
    finally:
        _keep_metered(gateway, calls, before, name, prompt)


def _invoke_aether(
    name: str,
    prompt: str,
    *,
    gateway: _Gateway | None,
    gateway_url: str,
    token: str,
    secret: str,
    alias: str,
    complete: Callable[[str], str] | None,
    calls: list[dict[str, Any]],
) -> str:
    if complete is not None:
        text = complete(prompt)
        _append_injected(calls, name, prompt)
        return text
    if gateway is None:
        raise RuntimeError("gateway_missing")
    before = len(gateway.calls)
    try:
        return _post_gateway(
            gateway_url,
            {"model": alias, "messages": [{"role": "user", "content": prompt}], "max_tokens": MAX_OUTPUT_TOKENS},
            token,
            secret,
        )
    finally:
        _keep_metered(gateway, calls, before, name, prompt)


def _invoke_group(
    name: str,
    prompt: str,
    *,
    learned: bool,
    home: Path,
    work: Path,
    gateway: _Gateway | None,
    gateway_url: str,
    token: str,
    secret: str,
    alias: str,
    binary: Path,
    launch: Callable[[str, dict[str, str], Path], str] | None,
    complete: Callable[[str], str] | None,
    calls: list[dict[str, Any]],
) -> str:
    if name.startswith("hermes"):
        return _invoke_hermes(
            name,
            prompt,
            learned=learned,
            home=home,
            work=work,
            gateway=gateway,
            gateway_url=gateway_url,
            token=token,
            secret=secret,
            binary=binary,
            launch=launch,
            calls=calls,
        )
    return _invoke_aether(
        name,
        prompt,
        gateway=gateway,
        gateway_url=gateway_url,
        token=token,
        secret=secret,
        alias=alias,
        complete=complete,
        calls=calls,
    )


def _base_report(status: str, **extra: Any) -> dict[str, Any]:
    report = {
        "status": status,
        "comparative_evidence": "not_run",
        "superior": None,
        "scripted_substitute": False,
        "caps": _caps(),
        "calls": 0,
        "calls_detail": [],
        "groups": {},
        "hermes_identity": _unchecked_identity(),
    }
    report.update(extra)
    return report


def _blocked(missing: list[str]) -> dict[str, Any]:
    return _base_report("blocked", missing=missing)


def _run_command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, **kwargs)


def hermes_identity(binary: Path, *, run: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> dict[str, Any]:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    runner = run or _run_command
    try:
        checkout = binary.resolve().parents[2]
    except IndexError:
        checkout = binary.parent
    home = Path(tempfile.mkdtemp(prefix="aether-hermes-id-"))
    probe = _probe_env(home)
    try:
        git = runner(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=WALL_SECONDS,
            check=False,
            env=probe,
        )
        version = runner(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=WALL_SECONDS,
            check=False,
            env=probe,
        )
    except (OSError, subprocess.TimeoutExpired):
        git = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        version = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    head = (git.stdout or "").strip()
    line = (version.stdout or "").splitlines()[0] if version.stdout else ""
    prefix = f"Hermes Agent v{lock['version']} "
    matches = bool(
        git.returncode == 0
        and version.returncode == 0
        and head == lock["commit"]
        and line.startswith(prefix)
        and str(lock["commit"])[:8] in line
    )
    return {
        "status": "verified" if matches else "mismatch",
        "matches": matches,
        "commit": head or None,
        "expected_commit": lock["commit"],
        "version": lock["version"],
        "version_line": line,
        "path": str(binary),
    }


def _missing_cli(dev: dict[str, Any], holdout_row: dict[str, Any]) -> dict[str, Any]:
    return _base_report(
        "incomplete",
        reason="hermes_cli_missing",
        dev=dev,
        holdout=holdout_row,
        unmatched=list(UNMATCHED),
    )


def _identity_mismatch(dev: dict[str, Any], holdout_row: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    return _base_report(
        "incomplete",
        reason="hermes_identity_mismatch",
        dev=dev,
        holdout=holdout_row,
        unmatched=list(UNMATCHED),
        hermes_identity=identity,
    )


def _record_group(name: str, prompt: str, text: str, calls: list[dict[str, Any]], start: int, learned: bool) -> dict[str, Any]:
    graded = grade_answer(text)
    group_calls = calls[start:]
    cost_known = bool(group_calls) and all(row.get("cost_status") == "known" for row in group_calls)
    return {
        "status": "ran",
        "learned": learned,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "response_excerpt": text[:500],
        "success": graded["success"],
        "answer": graded["answer"],
        "abstained": graded["abstained"],
        "false_completion": graded["false_completion"],
        "cost_status": "known" if cost_known else "unknown",
        "cost_micro_usd": sum(int(row["cost_micro_usd"]) for row in group_calls) if cost_known else None,
    }


def _fail_group(exc: BaseException, secret: str, calls: list[dict[str, Any]], start: int) -> dict[str, Any]:
    group_calls = calls[start:]
    return {
        "status": "failed",
        "reason": redact_secret(f"{type(exc).__name__}:{exc}", secret)[-300:],
        "success": False,
        "cost_status": "unknown",
        "false_completion": False,
        "abstained": False,
        "call_ids": [row.get("id") for row in group_calls],
        "held_tokens": _held_total(group_calls),
    }


def _prepare_context(
    *,
    dev_cases: Path | None,
    repo_root: Path | None,
    holdout: Path | None,
    launch: Callable[[str, dict[str, str], Path], str] | None,
    injected: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = repo_root or ROOT
    cases = dev_cases or DEV_CASES
    dev = load_dev_cases(cases)
    holdout_row = holdout_status(holdout, repo_root=root)
    if holdout_row["status"] == "rejected":
        return (
            {
                "status": "failed",
                "comparative_evidence": "not_run",
                "superior": None,
                "reason": "holdout_inside_worktree",
                "dev": dev,
                "holdout": holdout_row,
                "scripted_substitute": injected,
                "caps": _caps(),
                "calls": 0,
                "calls_detail": [],
                "groups": {},
                "hermes_identity": _unchecked_identity(),
            },
            dev,
            holdout_row,
            _unchecked_identity(),
        )
    if launch is not None:
        return None, dev, holdout_row, _unchecked_identity()
    if not HERMES_BIN.is_file():
        return _missing_cli(dev, holdout_row), dev, holdout_row, _unchecked_identity()
    identity = hermes_identity(HERMES_BIN)
    if not identity["matches"]:
        return _identity_mismatch(dev, holdout_row, identity), dev, holdout_row, identity
    return None, dev, holdout_row, identity


def _run_groups(
    *,
    gateway: _Gateway | None,
    gateway_url: str,
    token: str,
    secret: str,
    alias: str,
    launch: Callable[[str, dict[str, str], Path], str] | None,
    complete: Callable[[str], str] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, tuple[int, int]], str | None]:
    calls: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    spans: dict[str, tuple[int, int]] = {}
    error: str | None = None
    try:
        for name in GROUPS:
            learned = name.endswith("learned")
            prompt = prompt_for(learned)
            if len(calls) >= MAX_CALLS or (gateway is not None and not gateway.allow()):
                groups[name] = {"status": "not_run", "reason": "cap", "success": None, "cost_status": "unknown"}
                continue
            start = len(calls)
            home = Path(tempfile.mkdtemp(prefix="aether-hermes-"))
            try:
                text = _invoke_group(
                    name,
                    prompt,
                    learned=learned,
                    home=home,
                    work=home / "work",
                    gateway=gateway,
                    gateway_url=gateway_url,
                    token=token,
                    secret=secret,
                    alias=alias,
                    binary=HERMES_BIN,
                    launch=launch,
                    complete=complete,
                    calls=calls,
                )
                if secret and secret in text:
                    raise RuntimeError("secret_in_output")
                groups[name] = _record_group(name, prompt, text, calls, start, learned)
                spans[name] = (start, len(calls))
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                groups[name] = _fail_group(exc, secret, calls, start)
            finally:
                if home.exists():
                    shutil.rmtree(home, ignore_errors=True)
    except (RuntimeError, OSError) as exc:
        error = redact_secret(f"{type(exc).__name__}:{exc}", secret)
    return calls, groups, spans, error


def _status_for(error: str | None, groups: dict[str, dict[str, Any]]) -> str:
    ran = [name for name in GROUPS if groups.get(name, {}).get("status") == "ran"]
    if error:
        return "failed"
    if len(ran) == len(GROUPS):
        return "probed"
    if ran:
        return "incomplete"
    return "failed"


def _finish_report(
    *,
    calls: list[dict[str, Any]],
    groups: dict[str, dict[str, Any]],
    spans: dict[str, tuple[int, int]],
    error: str | None,
    secret: str,
    alias: str,
    injected: bool,
    dev: dict[str, Any],
    holdout_row: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    for index, row in enumerate(calls, start=1):
        if row.get("source") == "injected":
            row["id"] = f"call-{index}"
    for name, (start, end) in spans.items():
        groups[name]["call_ids"] = [row.get("id") for row in calls[start:end]]
    rows = {name: [groups[name]] if name in groups and groups[name].get("success") is not None else [] for name in GROUPS}
    summary = summarize(rows)
    summary["superior"] = None
    summary["reason"] = "公开开发切片每组最多一条，费用未知时不删行。这次不能宣布领先。"
    report = {
        "status": _status_for(error, groups),
        "comparative_evidence": "not_run",
        "superior": None,
        "reason": summary["reason"],
        "scripted_substitute": injected,
        "caps": _caps(),
        "model_alias": alias,
        "model_base": os.environ.get(LIVE_BASE),
        "dev": {**dev, "executed_case_id": CASE_ID, "cases_sent_to_model": 1, "dev_file_cases_sent": False},
        "holdout": holdout_row,
        "unmatched": list(UNMATCHED),
        "hermes_identity": identity,
        "groups": groups,
        "calls": len(calls),
        "calls_detail": calls,
        "summary": summary,
        "error": error,
    }
    return _redact(report, secret)


def run_four_way(
    *,
    dev_cases: Path | None = None,
    repo_root: Path | None = None,
    holdout: Path | None = None,
    launch: Callable[[str, dict[str, str], Path], str] | None = None,
    complete: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    injected = launch is not None or complete is not None
    missing = [name for name in (LIVE_KEY, LIVE_BASE, LIVE_ALIAS) if not os.environ.get(name)]
    if missing and not injected:
        return _blocked(missing)
    secret = os.environ.get(LIVE_KEY, "")
    alias = os.environ.get(LIVE_ALIAS, MODEL_NAME)
    early, dev, holdout_row, identity = _prepare_context(
        dev_cases=dev_cases, repo_root=repo_root, holdout=holdout, launch=launch, injected=injected
    )
    if early is not None:
        return _redact(early, secret)
    token = secrets.token_hex(16)
    gateway: _Gateway | None = None
    thread: threading.Thread | None = None
    gateway_url = "http://injected/v1"
    calls: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    spans: dict[str, tuple[int, int]] = {}
    error: str | None = None
    try:
        if not injected:
            gateway = _Gateway(_endpoint(os.environ[LIVE_BASE].rstrip("/")), secret, alias)
            os.environ["AETHER_GATEWAY_TOKEN"] = token
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            gateway_url = f"http://127.0.0.1:{gateway.server_address[1]}/v1"
        calls, groups, spans, error = _run_groups(
            gateway=gateway,
            gateway_url=gateway_url,
            token=token,
            secret=secret,
            alias=alias,
            launch=launch,
            complete=complete,
        )
    except (RuntimeError, OSError) as exc:
        error = redact_secret(f"{type(exc).__name__}:{exc}", secret)
    finally:
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
        if thread is not None:
            thread.join(timeout=2)
        os.environ.pop("AETHER_GATEWAY_TOKEN", None)
    return _finish_report(
        calls=calls,
        groups=groups,
        spans=spans,
        error=error,
        secret=secret,
        alias=alias,
        injected=injected,
        dev=dev,
        holdout_row=holdout_row,
        identity=identity,
    )


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return redact_secret(value, secret)
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value
