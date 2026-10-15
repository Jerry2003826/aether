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
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from aether_worker.hermes_baseline import GROUPS, holdout_status, load_dev_cases, summarize
from aether_worker.live_harness import LIVE_ALIAS, LIVE_BASE, LIVE_KEY, _NoRedirect, _endpoint, _usage, redact_secret

ROOT = Path(__file__).resolve().parents[3]
DEV_CASES = ROOT / "evals" / "dev_cases.jsonl"
HERMES_BIN = ROOT / ".hermes" / ".venv" / "bin" / "hermes"
RESULT_NAME = "hermes_compare_result.json"
MAX_CALLS = 8
MAX_TOTAL_TOKENS = 8000
MAX_OUTPUT_TOKENS = 128
TASK = "计算 1+2。只回复一个 JSON 对象，键 answer，值是整数。不要其他文字。"
LESSON = "冻结笔记：若题目要求只回复 JSON，则不要加解释或代码块。"
CASE_ID = "DEV-COMPARE-SUM"
UNMATCHED = [
    "Hermes 用钉住的 CLI 单次 chat --oneshot，AETHER 这侧是同一网关的一次 chat completion，不是 Temporal 任务。",
    "两边都不读 holdout。公开开发集只记录哈希，不把 28 条规格说明发给模型。",
    "learned 组看到同一段冻结笔记；cold 组看不到。Hermes 另外把笔记写进独立 HERMES_HOME，AETHER 没有那套记忆目录。",
    "Hermes 进程在宿主机上，以便访问本机网关。提供者密钥不进入该进程。",
]


def _caps() -> dict[str, int]:
    return {"max_calls": MAX_CALLS, "max_total_tokens": MAX_TOTAL_TOKENS, "max_output_tokens": MAX_OUTPUT_TOKENS, "spend_cap_usd": 1}


def prompt_for(learned: bool) -> str:
    if learned:
        return LESSON + "\n" + TASK
    return TASK


def grade_answer(text: str) -> dict[str, Any]:
    answer = None
    start = text.rfind("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            payload = json.loads(text[start : end + 1])
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
    dropped = sorted(set(payload) - {"model", "messages", "max_tokens", "temperature"})
    requested = payload.get("max_tokens", MAX_OUTPUT_TOKENS)
    max_tokens = requested if isinstance(requested, int) and 1 <= requested <= MAX_OUTPUT_TOKENS else MAX_OUTPUT_TOKENS
    forwarded = {"model": alias, "messages": messages, "max_tokens": max_tokens, "temperature": 0}
    return json.dumps(forwarded).encode(), dropped


class _Gateway(ThreadingHTTPServer):
    def __init__(self, upstream: str, key: str, alias: str) -> None:
        self.upstream = upstream
        self.key = key
        self.alias = alias
        self.calls: list[dict[str, Any]] = []
        self.total_tokens = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def allow(self) -> bool:
        with self._lock:
            return len(self.calls) < MAX_CALLS and self.total_tokens < MAX_TOTAL_TOKENS


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server: _Gateway = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        started = time.perf_counter()
        if self.headers.get("Authorization") != f"Bearer {os.environ.get('AETHER_GATEWAY_TOKEN', '')}":
            self._send(401, {"error": "unauthorized"})
            return
        if not server.allow():
            self._send(429, {"error": "cap"})
            return
        try:
            forwarded, dropped = _forward_body(raw, server.alias)
        except (RuntimeError, json.JSONDecodeError):
            self._send(400, {"error": "rejected"})
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
            detail = redact_secret(exc.read().decode("utf-8", errors="replace")[:300], server.key)
            self._send(exc.code, {"error": detail})
            return
        if server.key and server.key.encode() in body:
            body = body.replace(server.key.encode(), b"[REDACTED]")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._send(502, {"error": "response_not_json"})
            return
        if not isinstance(payload, dict):
            self._send(502, {"error": "response_not_object"})
            return
        usage = _usage(payload)
        counted = usage["total_tokens"] if isinstance(usage["total_tokens"], int) else MAX_OUTPUT_TOKENS
        content = ""
        try:
            content = str(payload["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError):
            content = ""
        with server._lock:
            server.total_tokens += counted
            server.calls.append(
                {
                    "id": f"call-{len(server.calls) + 1}",
                    "upstream_id": payload.get("id") if isinstance(payload.get("id"), str) else None,
                    "http_status": status,
                    "model": server.alias,
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
            )
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


def _child_env(home: Path, gateway: str, token: str, secret: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop(LIVE_KEY, None)
    if secret:
        for name, value in list(env.items()):
            if secret in str(value):
                env.pop(name, None)
    env["HERMES_HOME"] = str(home)
    env["HERMES_INFERENCE_MODEL"] = "deepseek-chat"
    env["OPENAI_BASE_URL"] = gateway
    env["OPENAI_API_KEY"] = token
    env["CUSTOM_BASE_URL"] = gateway
    env["CUSTOM_API_KEY"] = token
    return env


def _write_home(home: Path, gateway: str, token: str, *, learned: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: deepseek-chat\n  base_url: " + gateway + "\n",
        encoding="utf-8",
    )
    (home / ".env").write_text(f"CUSTOM_API_KEY={token}\nOPENAI_API_KEY={token}\n", encoding="utf-8")
    if learned:
        note = home / "memories"
        note.mkdir()
        (note / "frozen.txt").write_text(LESSON + "\n", encoding="utf-8")


def _hermes_argv(binary: Path, prompt: str) -> list[str]:
    return [
        str(binary),
        "chat",
        "-q",
        prompt,
        "--oneshot",
        "--max-turns",
        "1",
        "--ignore-rules",
        "--quiet",
        "--run-budget",
        "45",
        "-m",
        "deepseek-chat",
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
        timeout=90,
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
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
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


def _blocked(missing: list[str]) -> dict[str, Any]:
    return {
        "status": "blocked",
        "comparative_evidence": "not_run",
        "superior": None,
        "missing": missing,
        "scripted_substitute": False,
        "caps": _caps(),
        "calls": 0,
        "calls_detail": [],
        "groups": {},
    }


def run_four_way(
    *,
    dev_cases: Path | None = None,
    repo_root: Path | None = None,
    holdout: Path | None = None,
    launch: Callable[[str, dict[str, str], Path], str] | None = None,
    complete: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    root = repo_root or ROOT
    cases = dev_cases or DEV_CASES
    injected = launch is not None or complete is not None
    missing = [name for name in (LIVE_KEY, LIVE_BASE, LIVE_ALIAS) if not os.environ.get(name)]
    if missing and not injected:
        return _blocked(missing)
    secret = os.environ.get(LIVE_KEY, "")
    alias = os.environ.get(LIVE_ALIAS, "deepseek-chat")
    dev = load_dev_cases(cases)
    holdout_row = holdout_status(holdout, repo_root=root)
    if holdout_row["status"] == "rejected":
        return {
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
        }
    binary = HERMES_BIN
    if launch is None and not binary.is_file():
        return {
            "status": "incomplete",
            "comparative_evidence": "not_run",
            "superior": None,
            "reason": "hermes_cli_missing",
            "dev": dev,
            "holdout": holdout_row,
            "scripted_substitute": False,
            "caps": _caps(),
            "calls": 0,
            "calls_detail": [],
            "groups": {},
            "unmatched": list(UNMATCHED),
        }
    token = secrets.token_hex(16)
    calls: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    gateway: _Gateway | None = None
    thread: threading.Thread | None = None
    gateway_url = "http://injected/v1"
    error: str | None = None
    spans: dict[str, tuple[int, int]] = {}
    try:
        if not injected:
            gateway = _Gateway(_endpoint(os.environ[LIVE_BASE].rstrip("/")), secret, alias)
            os.environ["AETHER_GATEWAY_TOKEN"] = token
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            gateway_url = f"http://127.0.0.1:{gateway.server_address[1]}/v1"
        for name in GROUPS:
            learned = name.endswith("learned")
            prompt = prompt_for(learned)
            if len(calls) >= MAX_CALLS or (gateway is not None and not gateway.allow()):
                groups[name] = {"status": "not_run", "reason": "cap", "success": None, "cost_status": "unknown"}
                continue
            start = len(calls)
            home = Path(os.environ.get("TMPDIR", "/tmp")) / f"aether-hermes-{name}-{token[:8]}"
            work = home / "work"
            try:
                if name.startswith("hermes"):
                    child = _child_env(home, gateway_url, token, secret)
                    if launch is not None:
                        text = launch(prompt, child, home)
                        calls.append({"id": "", "group": name, "source": "injected", "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "cost_status": "unknown", "total_tokens": None})
                    else:
                        assert gateway is not None
                        _write_home(home, gateway_url, token, learned=learned)
                        work.mkdir(parents=True, exist_ok=True)
                        before_gateway = len(gateway.calls)
                        text = _default_hermes(binary, prompt, child, work)
                        if len(gateway.calls) == before_gateway:
                            raise RuntimeError("no_metered_call")
                        _stamp(gateway.calls, before_gateway, name, prompt)
                        calls.extend(gateway.calls[before_gateway:])
                else:
                    if complete is not None:
                        text = complete(prompt)
                        calls.append({"id": "", "group": name, "source": "injected", "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "cost_status": "unknown", "total_tokens": None})
                    else:
                        assert gateway is not None
                        before_gateway = len(gateway.calls)
                        text = _post_gateway(
                            gateway_url,
                            {"model": alias, "messages": [{"role": "user", "content": prompt}], "max_tokens": MAX_OUTPUT_TOKENS},
                            token,
                            secret,
                        )
                        _stamp(gateway.calls, before_gateway, name, prompt)
                        calls.extend(gateway.calls[before_gateway:])
                if secret and secret in text:
                    raise RuntimeError("secret_in_output")
                graded = grade_answer(text)
                group_calls = calls[start:]
                cost_known = bool(group_calls) and all(row.get("cost_status") == "known" for row in group_calls)
                groups[name] = {
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
                spans[name] = (start, len(calls))
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                groups[name] = {
                    "status": "failed",
                    "reason": redact_secret(f"{type(exc).__name__}:{exc}", secret)[-300:],
                    "success": False,
                    "cost_status": "unknown",
                    "false_completion": False,
                    "abstained": False,
                }
            finally:
                if home.exists():
                    shutil.rmtree(home, ignore_errors=True)
    except Exception as exc:
        error = redact_secret(f"{type(exc).__name__}:{exc}", secret)
    finally:
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
        if thread is not None:
            thread.join(timeout=2)
        os.environ.pop("AETHER_GATEWAY_TOKEN", None)
    for index, row in enumerate(calls, start=1):
        if row.get("source") == "injected":
            row["id"] = f"call-{index}"
    for name, (start, end) in spans.items():
        groups[name]["call_ids"] = [row.get("id") for row in calls[start:end]]
    rows = {name: [groups[name]] if name in groups and groups[name].get("success") is not None else [] for name in GROUPS}
    summary = summarize(rows)
    summary["superior"] = None
    summary["reason"] = "公开开发切片每组最多一条，费用未知时不删行。这次不能宣布领先。"
    ran = [name for name in GROUPS if groups.get(name, {}).get("status") == "ran"]
    if error:
        status = "failed"
    elif len(ran) == len(GROUPS):
        status = "probed"
    elif ran:
        status = "incomplete"
    else:
        status = "failed"
    report = {
        "status": status,
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
        "groups": groups,
        "calls": len(calls),
        "calls_detail": calls,
        "summary": summary,
        "error": error,
    }
    return _redact(report, secret)


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return redact_secret(value, secret)
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value
