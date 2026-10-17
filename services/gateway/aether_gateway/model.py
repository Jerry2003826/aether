"""Job-scoped model calls. The provider key stays in this process."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Any

import litellm
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DataError, IntegrityError

from aether_domain.security import sha256_text
from aether_storage.budget import apply_reservation_extension, reservation_can_grow
from aether_storage.db import Database

litellm.suppress_debug_info = True

# LiteLLM's response_cost is looked up in its bundled price table by the model
# name the upstream reports. It is a local estimate, not a provider bill.
COST_SOURCE = "local_price_estimate"
PRICING_VERSION = f"litellm-{version('litellm')}"
CODING_CALL_OUTPUT_CAP = 256
CODING_MAX_MODEL_CALLS = 4
# One upstream answer is read under one overall deadline and one byte cap. The per-read
# socket timeout alone lets a slow drip (a byte now and then) hold a thread and its buffer.
UPSTREAM_DEADLINE_SECONDS = 120.0
UPSTREAM_MAX_BYTES = 8 * 1_048_576
_UPSTREAM_READ_CHUNK = 65_536
# Upper-bound guess of input tokens from request bytes. Real tokenizers average
# more than two bytes per token for English and code, so this over-reserves; the
# measured usage settles the difference after the call.
INPUT_BYTES_PER_TOKEN = 2


def upstream_deadline_seconds() -> float:
    """AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS, or the default when unset, not a finite positive number."""
    raw = os.environ.get("AETHER_GATEWAY_UPSTREAM_DEADLINE_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return UPSTREAM_DEADLINE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return UPSTREAM_DEADLINE_SECONDS
    return value


def upstream_max_bytes() -> int:
    """AETHER_GATEWAY_UPSTREAM_MAX_BYTES, or the default when unset or not a positive integer."""
    raw = os.environ.get("AETHER_GATEWAY_UPSTREAM_MAX_BYTES", "").strip()
    if not raw.isdigit():
        return UPSTREAM_MAX_BYTES
    value = int(raw)
    return value if value > 0 else UPSTREAM_MAX_BYTES


def _read_cut(reason: str) -> GatewayDenied:
    """A response cut short was sent and may be billed; it settles like any other upstream error."""
    _logger().warning("upstream_read_cut reason=%s", reason)
    return GatewayDenied("upstream_error", sent=True)


def _response_socket(response: Any) -> Any:
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if hasattr(sock, "settimeout") else None


def _read_bounded(response: Any, *, deadline: float, limit: int, per_read: float) -> bytes:
    """Read the whole body before deadline (time.monotonic) and within limit bytes.

    Each socket read waits at most the time left, so the deadline covers the whole body,
    not each read. A declared Content-Length above the cap is refused before reading.
    """
    declared = response.headers.get("content-length") if getattr(response, "headers", None) is not None else None
    if declared is not None and str(declared).strip().isdigit() and int(str(declared).strip()) > limit:
        raise _read_cut("size")
    sock = _response_socket(response)
    chunks: list[bytes] = []
    total = 0
    while True:
        if getattr(response, "fp", None) is None:
            # http.client drops fp once the declared or chunked body is complete.
            return b"".join(chunks)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _read_cut("deadline")
        if sock is not None:
            sock.settimeout(max(0.001, min(per_read, remaining)))
        try:
            chunk = response.read1(min(_UPSTREAM_READ_CHUNK, limit - total + 1))
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise _read_cut("deadline") from None
            raise
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _read_cut("size")
        chunks.append(chunk)


def estimate_input_tokens(prompt: str) -> int:
    """Tokens held for the request body before it is sent. Never zero."""
    size = len(prompt.encode("utf-8"))
    return max(1, -(-size // INPUT_BYTES_PER_TOKEN))


def coding_reservation_gap(
    *,
    reserved_tokens: int,
    measured_tokens: int,
    pending_tokens: int,
    call_reserve_tokens: int | None,
    call_tokens: int = CODING_CALL_OUTPUT_CAP,
) -> int | None:
    """Tokens to append so the next call fits. None means this reservation does not grow.

    call_tokens is this call's estimated input plus the output cap.
    """
    if call_reserve_tokens is None or call_reserve_tokens < 1:
        return None
    allowance = max(call_reserve_tokens, call_tokens)
    need = measured_tokens + pending_tokens + allowance
    if reserved_tokens >= need:
        return 0
    return need - reserved_tokens


def coding_reservation_micro(tokens: int, *, call_tokens: int, call_micro: int) -> int:
    if tokens < 1:
        return 0
    if call_tokens < 1 or call_micro < 1:
        return tokens
    return (tokens * call_micro + call_tokens - 1) // call_tokens


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value
_CHAT_FIELDS = ("model", "messages", "tools", "tool_choice")
_CODING_LEASE_STATES = ("STARTING", "ACTIVE", "EXPORTING")
_MAX_FENCING = 9223372036854775807
_SAFE_ERROR = re.compile(r"^[a-z0-9_]{1,64}$")
_LOG = logging.getLogger("aether.gateway.model")


def _is_timeout(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "timeout" in text or "timed out" in text


def _cost_micro_usd(response: Any) -> int | None:
    hidden = getattr(response, "_hidden_params", None) or {}
    cost = hidden.get("response_cost") if isinstance(hidden, dict) else None
    if cost is None:
        cost = getattr(response, "response_cost", None)
    if cost is None:
        return None
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return int(round(value * 1_000_000))


def _redact(value: str, secret: str) -> str:
    if not secret:
        return value
    return value.replace(secret, "[REDACTED]")


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _alias_matches(model: str, aliases: list[str]) -> bool:
    if model in aliases:
        return True
    stripped = model.removeprefix("openai/")
    return bool(stripped) and stripped != model and stripped in aliases


def _chat_url(base: str) -> str:
    root = base.rstrip("/")
    if root.endswith("/v1"):
        return root + "/chat/completions"
    return root + "/v1/chat/completions"


def accepted_chat_body(body: dict[str, Any], *, max_tokens: int | None = None) -> dict[str, Any]:
    """Fields the coding relay may hash. timeout, stream and any other key are dropped."""
    cleaned = {key: body[key] for key in _CHAT_FIELDS if key in body}
    if max_tokens is not None:
        cleaned["max_tokens"] = max_tokens
    return cleaned


def chat_prompt(body: dict[str, Any]) -> str:
    return json.dumps(accepted_chat_body(body), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def upstream_model_name(requested: str) -> str:
    """Send the configured provider model. Admission still uses the sandbox alias."""
    alias = os.environ.get("AETHER_LIVE_MODEL_ALIAS", "").strip()
    return alias or requested


def upstream_chat_payload(body: dict[str, Any], *, max_tokens: int | None) -> dict[str, Any]:
    """Body posted upstream. A stream forces include_usage; the prompt hash ignores it."""
    payload = accepted_chat_body(body, max_tokens=max_tokens)
    payload["model"] = upstream_model_name(str(payload.get("model") or ""))
    if body.get("stream") is True:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def usage_counts(usage: object) -> tuple[int, int] | None:
    """Prompt and completion tokens from a JSON object or the last SSE usage chunk."""
    if not isinstance(usage, dict):
        return None
    incoming = _usage_token(usage, "prompt_tokens", "input_tokens")
    outgoing = _usage_token(usage, "completion_tokens", "output_tokens")
    if incoming is None or outgoing is None:
        return None
    return incoming, outgoing


def parse_upstream_chat(raw: bytes, content_type: str) -> dict[str, Any]:
    """One chat completion. SSE is folded into that object, including a final usage chunk."""
    if _is_event_stream(raw, content_type):
        return _aggregate_sse(raw)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GatewayDenied("upstream_error", sent=True) from exc
    if not isinstance(loaded, dict):
        raise GatewayDenied("upstream_error", sent=True)
    return loaded


def _usage_token(usage: dict[str, Any], *names: str) -> int | None:
    for name in names:
        if name not in usage:
            continue
        value = usage[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value
    return None


def _is_event_stream(raw: bytes, content_type: str) -> bool:
    if "text/event-stream" in content_type.lower():
        return True
    return raw.lstrip().startswith(b"data:")


def _sse_events(raw: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", raw.decode("utf-8", errors="replace")):
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if payload == "[DONE]":
            continue
        try:
            loaded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            events.append(loaded)
    return events


def _aggregate_sse(raw: bytes) -> dict[str, Any]:
    events = _sse_events(raw)
    if not events:
        raise GatewayDenied("upstream_error", sent=True)
    content: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    usage: dict[str, Any] | None = None
    model = ""
    completion_id = "chatcmpl-stream"
    role = "assistant"
    for event in events:
        if isinstance(event.get("id"), str) and event["id"] and completion_id == "chatcmpl-stream":
            completion_id = event["id"]
        if isinstance(event.get("model"), str) and event["model"]:
            model = event["model"]
        if usage_counts(event.get("usage")) is not None and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason:
            finish = reason
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        source = delta or message
        if isinstance(source.get("role"), str) and source["role"]:
            role = source["role"]
        piece = source.get("content")
        if isinstance(piece, str):
            content.append(piece)
        calls = source.get("tool_calls")
        if isinstance(calls, list):
            _merge_tool_calls(tool_calls, calls)
    message_out: dict[str, Any] = {"role": role, "content": "".join(content) if content else None}
    if tool_calls:
        message_out["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    result: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": message_out}],
    }
    if usage is not None:
        result["usage"] = usage
    return result


def _merge_tool_calls(found: dict[int, dict[str, Any]], calls: list[Any]) -> None:
    for item in calls:
        if not isinstance(item, dict):
            continue
        index = item.get("index", 0)
        if isinstance(index, bool) or not isinstance(index, int):
            index = 0
        current = found.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if isinstance(item.get("id"), str) and item["id"]:
            current["id"] = item["id"]
        if isinstance(item.get("type"), str) and item["type"]:
            current["type"] = item["type"]
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(name, str) and name:
            current["function"]["name"] += name
        if isinstance(arguments, str) and arguments:
            current["function"]["arguments"] += arguments


def _provider_error_code(body: str) -> str | None:
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    error = loaded.get("error")
    if not isinstance(error, dict):
        return None
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str) and _SAFE_ERROR.fullmatch(value):
            return value
    return None


def _logger() -> logging.Logger:
    _LOG.setLevel(logging.INFO)
    if not _LOG.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        _LOG.addHandler(handler)
    return _LOG


def _public(value: object, secret: str) -> str:
    text = "" if value is None else str(value)
    if secret and secret in text:
        return "[REDACTED]"
    if len(text) > 80:
        return text[:80]
    return text


def _completion_text(response: Any) -> tuple[str | None, str | None]:
    """Visible answer text and finish_reason.

    `finish_reason=length` means the provider cut the answer at max_tokens.
    That text is not a complete answer, so the visible text is omitted.
    """
    choice = response.choices[0]
    reason = getattr(choice, "finish_reason", None)
    finish = str(reason).strip().lower() if reason is not None and str(reason).strip() else None
    content = getattr(choice.message, "content", None) or ""
    if not isinstance(content, str):
        content = str(content)
    if finish == "length":
        return None, "length"
    return content, finish


def _choice_content(response: Any) -> str:
    content = getattr(response.choices[0].message, "content", None) or ""
    if not isinstance(content, str):
        return str(content)
    return content


class GatewayDenied(Exception):
    """sent is true once the request may have reached the provider."""

    def __init__(
        self,
        code: str,
        *,
        sent: bool = False,
        upstream_status: int | None = None,
        provider_error: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.sent = sent
        self.upstream_status = upstream_status
        self.provider_error = provider_error


class ModelGateway:
    def __init__(self, database: Database, *, upstream_base: str, provider_key: str, timeout: float = 5) -> None:
        self.database = database
        self.upstream_base = upstream_base
        self.provider_key = provider_key
        self.timeout = timeout
        self.audit: list[str] = []
        self._coding_child_key = ""
        self._coding_call_tokens = CODING_CALL_OUTPUT_CAP
        self._response_kind = "json"

    def issue_token(
        self,
        *,
        owner_id: str,
        project_id: str,
        task_id: str,
        request_key: str,
        attempt_id: str,
        capabilities: list[str],
        aliases: list[str],
        egress: list[str] | None = None,
        job_id: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        """A token for one booked request of one task attempt.

        complete() spends this token on the first admitted call. relay_chat keeps
        the session token and spends a new child token per call. owner_id stays in
        the stored scope so the gateway can open that child without reading projects.
        A coding session also stores the job and operation so the call is fenced by
        that job's lease instead of the task status.
        """
        token = secrets.token_urlsafe(32)
        scope = {
            "project_id": project_id,
            "task_id": task_id,
            "request_key": request_key,
            "attempt_id": attempt_id,
            "owner_id": owner_id,
            "capabilities": capabilities,
            "aliases": aliases,
            "egress": egress or [],
        }
        if job_id is not None:
            scope["job_id"] = job_id
        if operation_id is not None:
            scope["operation_id"] = operation_id
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO capability_grants (project_id, task_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
                    "VALUES (:project_id, :task_id, 'job', :token_hash, 'model', CAST(:scope AS jsonb), :expires_at, 'v1')"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "token_hash": sha256_text(token),
                    "scope": json.dumps(scope),
                    "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                },
            )
        return token

    def complete(self, token: str, body: dict[str, Any]) -> dict[str, Any]:
        grant = self._scope(token)
        scope = grant["scope"]
        alias = str(body.get("alias") or "")
        if alias not in scope.get("aliases", []):
            raise GatewayDenied("alias_denied")
        project_id = str(grant["project_id"])
        max_tokens = self._admit(grant, alias=alias, prompt=str(body.get("prompt") or ""), requested=body.get("max_tokens"))
        self.audit.append(f"model project={project_id} alias={alias}")
        call = {"max_tokens": max_tokens}
        try:
            response = litellm.completion(
                model=f"openai/{alias}",
                api_base=self.upstream_base,
                api_key=self.provider_key,
                messages=[{"role": "user", "content": str(body.get("prompt") or "")}],
                timeout=self.timeout,
                num_retries=0,
                **call,
            )
        except Exception as exc:
            self.audit.append(f"model project={project_id} alias={alias} outcome=failed")
            if _is_timeout(exc):
                return {
                    "project_id": project_id,
                    "task_id": scope.get("task_id"),
                    "requested_alias": alias,
                    "upstream_model": None,
                    "output": None,
                    "finish_reason": None,
                    "usage_status": "unknown",
                    "input_tokens": None,
                    "output_tokens": None,
                    "actual_cost_micro_usd": None,
                    "cost_status": "unknown",
                }
            raise GatewayDenied("upstream_error", sent=True) from exc
        visible, finish_reason = _completion_text(response)
        partial = _redact(_choice_content(response), self.provider_key)
        output = None if finish_reason == "length" else _redact(visible or "", self.provider_key)
        upstream_model = _redact(str(getattr(response, "model", "") or ""), self.provider_key)
        cost = _cost_micro_usd(response)
        usage = getattr(response, "usage", None)
        if usage is None or usage.prompt_tokens is None or usage.completion_tokens is None:
            result = {
                "project_id": project_id,
                "task_id": scope.get("task_id"),
                "requested_alias": alias,
                "upstream_model": upstream_model or None,
                "output": output,
                "finish_reason": finish_reason,
                "usage_status": "unknown",
                "input_tokens": None,
                "output_tokens": None,
                "actual_cost_micro_usd": None,
                "cost_status": "unknown",
            }
        else:
            result = {
                "project_id": project_id,
                "task_id": scope.get("task_id"),
                "requested_alias": alias,
                "upstream_model": upstream_model or None,
                "output": output,
                "finish_reason": finish_reason,
                "usage_status": "known",
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "actual_cost_micro_usd": cost,
                "cost_status": "estimated" if cost is not None else "unknown",
                "cost_source": COST_SOURCE if cost is not None else "unreported",
                "pricing_version": PRICING_VERSION if cost is not None else None,
            }
        self.audit.append(f"model project={project_id} alias={alias} outcome={result['usage_status']}")
        encoded = json.dumps(result)
        if self.provider_key in encoded or self.provider_key in partial or any(self.provider_key in line for line in self.audit):
            raise GatewayDenied("secret_leak", sent=True)
        return result

    def relay_chat(self, job_token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Forward one coding chat completion fenced by the coding job lease.

        The session token stays valid for the attempt. Each call spends a new
        one-time child grant. A repeat of that prompt is rejected. A call that
        cannot grow inside the task budget is rejected before the provider.
        The task row is not read or written.
        """
        grant = self._scope(job_token)
        scope = grant["scope"]
        capabilities = scope.get("capabilities") or []
        if "coding:model" not in capabilities:
            raise GatewayDenied("capability_denied")
        model = str(body.get("model") or "")
        aliases = [str(item) for item in scope.get("aliases") or []]
        if not _alias_matches(model, aliases):
            raise GatewayDenied("alias_denied")
        alias = model.removeprefix("openai/") or model
        if not self.upstream_base or not self.provider_key:
            # No upstream or no provider key means no model. Nothing scripted answers in its
            # place, so no file is changed and the job fails as model_unavailable instead of passing.
            self._note_admission_block(grant, "model_unavailable")
            self._log_relay(
                project_id=str(grant["project_id"]),
                status=None,
                requested_model=model,
                upstream_model="",
                stream=body.get("stream") is True,
                response_kind="",
                incoming=None,
                outgoing=None,
                error="model_unavailable",
            )
            raise GatewayDenied("model_unavailable", sent=False)
        prompt = chat_prompt(body)
        try:
            max_tokens = self._spend_coding_ticket(grant, alias=alias, prompt=prompt)
        except GatewayDenied as exc:
            if exc.code == "budget_exceeded":
                self._note_admission_block(grant, exc.code)
            self._log_relay(
                project_id=str(grant["project_id"]),
                status=None,
                requested_model=model,
                upstream_model="",
                stream=body.get("stream") is True,
                response_kind="",
                incoming=None,
                outgoing=None,
                error=exc.code,
            )
            raise
        self.audit.append(f"relay project={grant['project_id']} model={model}")
        payload = upstream_chat_payload(body, max_tokens=max_tokens)
        upstream_model = str(payload.get("model") or "")
        client_stream = body.get("stream") is True
        try:
            result = self._post_upstream(payload)
        except GatewayDenied as exc:
            self._note_upstream_failure(grant, exc, release_pending=True)
            self._log_relay(
                project_id=str(grant["project_id"]),
                status=exc.upstream_status,
                requested_model=model,
                upstream_model=upstream_model,
                stream=client_stream,
                response_kind=self._response_kind,
                incoming=None,
                outgoing=None,
                error=exc.code,
            )
            raise
        self._add_measured_usage(grant, result)
        encoded = json.dumps(result)
        if self.provider_key and (self.provider_key in encoded or any(self.provider_key in line for line in self.audit)):
            denied = GatewayDenied("secret_leak", sent=True)
            self._note_upstream_failure(grant, denied, release_pending=False)
            self._log_relay(
                project_id=str(grant["project_id"]),
                status=200,
                requested_model=model,
                upstream_model=upstream_model,
                stream=client_stream,
                response_kind=self._response_kind,
                incoming=None,
                outgoing=None,
                error=denied.code,
            )
            raise denied
        counted = usage_counts(result.get("usage"))
        self._log_relay(
            project_id=str(grant["project_id"]),
            status=200,
            requested_model=model,
            upstream_model=upstream_model,
            stream=client_stream,
            response_kind=self._response_kind,
            incoming=None if counted is None else counted[0],
            outgoing=None if counted is None else counted[1],
            error="" if counted is not None else "usage_missing",
        )
        return result

    def _spend_coding_ticket(self, session: dict[str, Any], *, alias: str, prompt: str) -> int:
        """Spend one child ticket while the coding job lease and fencing token still match.

        Replay and overrun stop here. This does not call _admit and does not change the task.
        """
        scope = session["scope"]
        project_id = str(session["project_id"])
        task_id = str(session["task_id"])
        parent_key = str(scope["request_key"])
        owner_id = scope.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id:
            raise GatewayDenied("budget_exceeded")
        job_id = _uuid_text(scope.get("job_id"))
        operation_id = _uuid_text(scope.get("operation_id"))
        fencing = _fencing_token(scope.get("attempt_id"))
        attempt_id = str(fencing)
        prompt_sha = sha256_text(prompt)
        call_tokens = estimate_input_tokens(prompt) + CODING_CALL_OUTPUT_CAP
        child_key = f"{parent_key}:call:{prompt_sha}"
        self._open_coding_ticket(
            project_id=project_id,
            task_id=task_id,
            parent_key=parent_key,
            child_key=child_key,
            attempt_id=attempt_id,
            alias=alias,
            prompt_sha=prompt_sha,
            job_id=job_id,
            operation_id=operation_id,
            fencing=fencing,
            call_tokens=call_tokens,
        )
        try:
            child_token = self.issue_token(
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                request_key=child_key,
                attempt_id=attempt_id,
                capabilities=["coding:model"],
                aliases=[alias],
                job_id=job_id,
                operation_id=operation_id,
            )
            self._revoke_once(self._scope(child_token))
            self._require_coding_lease(
                project_id=project_id,
                task_id=task_id,
                job_id=job_id,
                operation_id=operation_id,
                fencing=fencing,
            )
        except GatewayDenied:
            self._abort_coding_ticket(project_id, task_id, parent_key, child_key)
            raise
        self._close_coding_ticket(project_id, task_id, child_key)
        self._coding_child_key = child_key
        self._coding_call_tokens = call_tokens
        return CODING_CALL_OUTPUT_CAP

    def _require_coding_lease(
        self,
        *,
        project_id: str,
        task_id: str,
        job_id: str,
        operation_id: str,
        fencing: int,
    ) -> None:
        with self.database.transaction(project_id=project_id) as conn:
            if not _coding_lease_held(
                conn,
                project_id=project_id,
                task_id=task_id,
                job_id=job_id,
                operation_id=operation_id,
                fencing=fencing,
            ):
                raise GatewayDenied("token_rejected")

    def _revoke_once(self, grant: dict[str, Any]) -> None:
        with self.database.transaction(project_id=str(grant["project_id"])) as conn:
            updated = conn.execute(
                text(
                    "UPDATE capability_grants SET revoked_at = now() "
                    "WHERE project_id = :project_id AND id = :id AND revoked_at IS NULL AND expires_at > now()"
                ),
                {"project_id": grant["project_id"], "id": grant["id"]},
            ).rowcount
        if updated != 1:
            raise GatewayDenied("token_rejected")

    def _open_coding_ticket(
        self,
        *,
        project_id: str,
        task_id: str,
        parent_key: str,
        child_key: str,
        attempt_id: str,
        alias: str,
        prompt_sha: str,
        job_id: str,
        operation_id: str,
        fencing: int,
        call_tokens: int = CODING_CALL_OUTPUT_CAP,
    ) -> None:
        with self.database.transaction(project_id=project_id) as conn:
            if not _coding_lease_held(
                conn,
                project_id=project_id,
                task_id=task_id,
                job_id=job_id,
                operation_id=operation_id,
                fencing=fencing,
            ):
                raise GatewayDenied("token_rejected")
            account = conn.execute(
                text(
                    "SELECT id, limit_micro_usd, spent_micro_usd, held_micro_usd, token_limit, tokens_used, tokens_held, "
                    "max_requests, requests_admitted, verification_reserve_fraction FROM budget_accounts "
                    "WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().first()
            parent = conn.execute(
                text(
                    "SELECT usage_status, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": parent_key},
            ).mappings().first()
            existing = conn.execute(
                text(
                    "SELECT 1 FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": child_key},
            ).first()
            if existing is not None:
                raise GatewayDenied("token_rejected")
            if parent is None or parent["usage_status"] != "reserved":
                raise GatewayDenied("budget_exceeded")
            detail = parent["usage_detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            detail = dict(detail or {})
            reserved_tokens = _count(detail.get("reserved_tokens"))
            measured = _count(detail.get("measured_input_tokens")) + _count(detail.get("measured_output_tokens"))
            pending = _count(detail.get("pending_tokens"))
            call_reserve = _positive_int(detail.get("call_reserve_tokens"))
            gap = coding_reservation_gap(
                reserved_tokens=reserved_tokens,
                measured_tokens=measured,
                pending_tokens=pending,
                call_reserve_tokens=call_reserve,
                call_tokens=call_tokens,
            )
            if gap is None:
                if reserved_tokens < 1 or measured + pending + call_tokens > reserved_tokens:
                    raise GatewayDenied("budget_exceeded")
            elif gap > 0:
                call_micro = _positive_int(detail.get("call_reserve_micro_usd")) or call_reserve or gap
                micro = coding_reservation_micro(gap, call_tokens=call_reserve or gap, call_micro=call_micro)
                if account is None or not reservation_can_grow(dict(account), amount=micro, tokens=gap):
                    raise GatewayDenied("budget_exceeded")
                apply_reservation_extension(
                    conn,
                    account_id=account["id"],
                    project_id=project_id,
                    task_id=task_id,
                    request_key=parent_key,
                    amount=micro,
                    tokens=gap,
                    detail=detail,
                )
                _logger().info(
                    "coding_reserve_extended project=%s tokens=%s micro_usd=%s",
                    project_id,
                    gap,
                    micro,
                )
            detail["pending_tokens"] = pending + call_tokens
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {
                    "detail": json.dumps(detail),
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": parent_key,
                },
            )
            used = conn.execute(
                text(
                    "SELECT count(*) FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key LIKE :prefix"
                ),
                {"project_id": project_id, "task_id": task_id, "prefix": parent_key + ":call:%"},
            ).scalar()
            if int(used or 0) >= CODING_MAX_MODEL_CALLS:
                raise GatewayDenied("budget_exceeded")
            if int(used or 0) >= 1:
                # The job's own reservation admitted one request, which covers its first call.
                # Every later call is one more model request against max_requests.
                # A slot counted here is not refunded if the call is then never sent (lease
                # lost, upstream refused before contact): intentionally conservative.
                if account is None or int(account["requests_admitted"]) + 1 > int(account["max_requests"]):
                    raise GatewayDenied("budget_exceeded")
                conn.execute(
                    text("UPDATE budget_accounts SET requests_admitted = requests_admitted + 1, version = version + 1 WHERE id = :id"),
                    {"id": account["id"]},
                )
            child_detail = {
                "reserved_tokens": call_tokens,
                "input_estimate_tokens": call_tokens - CODING_CALL_OUTPUT_CAP,
                "dispatch_state": "sending",
                "dispatch_holder": attempt_id,
                "prompt_sha256": prompt_sha,
                "output_cap": CODING_CALL_OUTPUT_CAP,
            }
            try:
                conn.execute(
                    text(
                        "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status, usage_detail) "
                        "VALUES (:project_id, :task_id, :request_key, :digest, 'coding_call', :alias, 0, 'reserved', CAST(:detail AS jsonb))"
                    ),
                    {
                        "project_id": project_id,
                        "task_id": task_id,
                        "request_key": child_key,
                        "digest": prompt_sha,
                        "alias": alias,
                        "detail": json.dumps(child_detail),
                    },
                )
            except IntegrityError as exc:
                raise GatewayDenied("token_rejected") from exc

    def _close_coding_ticket(self, project_id: str, task_id: str, child_key: str) -> None:
        """The one-time row is spent. It no longer admits, and it does not add a second hold."""
        with self.database.transaction(project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                    "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": child_key},
            ).mappings().first()
            if row is None:
                return
            detail = row["usage_detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            detail = dict(detail or {})
            detail["dispatch_state"] = "response_recorded"
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = 'known', actual_micro_usd = 0, input_tokens = 0, output_tokens = 0, "
                    "usage_detail = CAST(:detail AS jsonb), completed_at = now() "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {
                    "detail": json.dumps(detail),
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": child_key,
                },
            )

    def _abort_coding_ticket(self, project_id: str, task_id: str, parent_key: str, child_key: str) -> None:
        """Drop the in-flight hold when the lease disappears before the provider is called."""
        with self.database.transaction(project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                    "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": child_key},
            ).mappings().first()
            if row is None:
                return
            parent = conn.execute(
                text(
                    "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                    "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": parent_key},
            ).mappings().first()
            if parent is not None:
                detail = parent["usage_detail"]
                if isinstance(detail, str):
                    detail = json.loads(detail)
                detail = dict(detail or {})
                held = _positive_int(_object_detail(row["usage_detail"]).get("reserved_tokens")) or CODING_CALL_OUTPUT_CAP
                detail["pending_tokens"] = max(0, _count(detail.get("pending_tokens")) - held)
                conn.execute(
                    text(
                        "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                        "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                    ),
                    {
                        "detail": json.dumps(detail),
                        "project_id": project_id,
                        "task_id": task_id,
                        "request_key": parent_key,
                    },
                )
            child = row["usage_detail"]
            if isinstance(child, str):
                child = json.loads(child)
            child = dict(child or {})
            child["dispatch_state"] = "response_recorded"
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = 'known', actual_micro_usd = 0, input_tokens = 0, output_tokens = 0, "
                    "usage_detail = CAST(:detail AS jsonb), completed_at = now() "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {
                    "detail": json.dumps(child),
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": child_key,
                },
            )
            conn.execute(
                text(
                    "UPDATE capability_grants SET revoked_at = now() "
                    "WHERE project_id = :project_id AND task_id = :task_id AND revoked_at IS NULL "
                    "AND scope->>'request_key' = :request_key"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": child_key},
            )

    def _note_admission_block(self, session: dict[str, Any], code: str) -> None:
        """Remember a pre-upstream rejection (budget_exceeded, model_unavailable) on the parent reservation.

        The worker reads it as the job's error code. The hold stays until measured usage is settled.
        """
        project_id = str(session["project_id"])
        task_id = str(session["task_id"])
        parent_key = str(session["scope"]["request_key"])
        with self.database.transaction(project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                    "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": parent_key},
            ).mappings().first()
            if row is None:
                return
            detail = _object_detail(row["usage_detail"])
            detail["admission_error"] = code
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {
                    "detail": json.dumps(detail),
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": parent_key,
                },
            )

    def _add_measured_usage(self, session: dict[str, Any], result: dict[str, Any]) -> None:
        counted = usage_counts(result.get("usage"))
        revision = result.get("model") if isinstance(result.get("model"), str) and result["model"] else None
        if revision and self.provider_key and self.provider_key in revision:
            revision = None
        project_id = str(session["project_id"])
        task_id = str(session["task_id"])
        parent_key = str(session["scope"]["request_key"])
        with self.database.transaction(project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                    "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": parent_key},
            ).mappings().first()
            if row is None:
                return
            detail = _object_detail(row["usage_detail"])
            if detail.get("proxy_drained") is True:
                # The job stopped waiting and already charged this call at its reservation.
                _logger().info("relay_late_answer_ignored project=%s", _public(project_id, self.provider_key))
                return
            detail["pending_tokens"] = max(0, _count(detail.get("pending_tokens")) - self._coding_call_tokens)
            if counted is None:
                detail["usage_missing"] = True
                self._note_unknown_call(detail)
            else:
                incoming, outgoing = counted
                detail["measured_input_tokens"] = _count(detail.get("measured_input_tokens")) + incoming
                detail["measured_output_tokens"] = _count(detail.get("measured_output_tokens")) + outgoing
                detail.pop("usage_missing", None)
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb), "
                    "model_revision = COALESCE(:revision, model_revision) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {
                    "detail": json.dumps(detail),
                    "revision": revision,
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": parent_key,
                },
            )
            self._write_child_observation(
                conn,
                project_id=project_id,
                task_id=task_id,
                incoming=None if counted is None else counted[0],
                outgoing=None if counted is None else counted[1],
                revision=revision,
                usage_missing=counted is None,
            )

    def _note_upstream_failure(self, session: dict[str, Any], exc: GatewayDenied, *, release_pending: bool) -> None:
        """Record the status and a safe error code. The provider message is not stored."""
        project_id = str(session["project_id"])
        task_id = str(session["task_id"])
        parent_key = str(session["scope"]["request_key"])
        with self.database.transaction(project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                    "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": parent_key},
            ).mappings().first()
            if row is None:
                return
            detail = _object_detail(row["usage_detail"])
            if detail.get("proxy_drained") is True:
                # Already charged at its reservation when the job stopped waiting for it.
                return
            detail["upstream_error"] = exc.code
            if exc.upstream_status is not None:
                detail["upstream_status"] = exc.upstream_status
            if exc.provider_error:
                detail["provider_error"] = exc.provider_error
            if release_pending:
                detail["pending_tokens"] = max(0, _count(detail.get("pending_tokens")) - self._coding_call_tokens)
                if exc.sent and exc.upstream_status is None:
                    # Sent but no answer (a timeout): the provider may bill it, so its hold is charged at settlement.
                    self._note_unknown_call(detail)
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {
                    "detail": json.dumps(detail),
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": parent_key,
                },
            )
            if self._coding_child_key:
                child = conn.execute(
                    text(
                        "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                        "AND request_key = :request_key FOR UPDATE"
                    ),
                    {"project_id": project_id, "task_id": task_id, "request_key": self._coding_child_key},
                ).mappings().first()
                if child is not None:
                    child_detail = _object_detail(child["usage_detail"])
                    child_detail["upstream_error"] = exc.code
                    if exc.upstream_status is not None:
                        child_detail["upstream_status"] = exc.upstream_status
                    if exc.provider_error:
                        child_detail["provider_error"] = exc.provider_error
                    conn.execute(
                        text(
                            "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                            "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                        ),
                        {
                            "detail": json.dumps(child_detail),
                            "project_id": project_id,
                            "task_id": task_id,
                            "request_key": self._coding_child_key,
                        },
                    )

    def _note_unknown_call(self, detail: dict[str, Any]) -> None:
        """Count a call whose usage never arrived and keep its reservation for settlement.

        These counters only grow: a later call that reports usage does not erase them.
        """
        detail["unknown_usage_calls"] = _count(detail.get("unknown_usage_calls")) + 1
        detail["unknown_reserved_tokens"] = _count(detail.get("unknown_reserved_tokens")) + self._coding_call_tokens

    def _write_child_observation(
        self,
        conn: Connection,
        *,
        project_id: str,
        task_id: str,
        incoming: int | None,
        outgoing: int | None,
        revision: str | None,
        usage_missing: bool,
    ) -> None:
        if not self._coding_child_key:
            return
        row = conn.execute(
            text(
                "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                "AND request_key = :request_key FOR UPDATE"
            ),
            {"project_id": project_id, "task_id": task_id, "request_key": self._coding_child_key},
        ).mappings().first()
        if row is None:
            return
        detail = _object_detail(row["usage_detail"])
        if usage_missing:
            detail["usage_missing"] = True
        else:
            detail.pop("usage_missing", None)
        conn.execute(
            text(
                "UPDATE model_requests SET input_tokens = COALESCE(:incoming, input_tokens), "
                "output_tokens = COALESCE(:outgoing, output_tokens), model_revision = COALESCE(:revision, model_revision), "
                "usage_detail = CAST(:detail AS jsonb) "
                "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
            ),
            {
                "incoming": incoming,
                "outgoing": outgoing,
                "revision": revision,
                "detail": json.dumps(detail),
                "project_id": project_id,
                "task_id": task_id,
                "request_key": self._coding_child_key,
            },
        )

    def _post_upstream(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            _chat_url(self.upstream_base),
            data=json.dumps(body).encode(),
            headers={"authorization": f"Bearer {self.provider_key}", "content-type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        window = upstream_deadline_seconds()
        deadline = time.monotonic() + window
        try:
            with opener.open(request, timeout=min(self.timeout, window)) as response:
                raw = _read_bounded(response, deadline=deadline, limit=upstream_max_bytes(), per_read=self.timeout)
                content_type = response.headers.get("content-type", "")
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if self.provider_key and self.provider_key in detail:
                raise GatewayDenied("secret_leak", sent=True) from exc
            status_code = exc.code if isinstance(exc.code, int) else 0
            provider = _provider_error_code(detail)
            code = f"upstream_http_{status_code}" if 400 <= status_code <= 599 else "upstream_error"
            raise GatewayDenied(code, sent=True, upstream_status=status_code or None, provider_error=provider) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise GatewayDenied("upstream_error", sent=_is_timeout(exc)) from exc
        self._response_kind = "sse" if _is_event_stream(raw, content_type) else "json"
        decoded = raw.decode(errors="replace")
        if self.provider_key and self.provider_key in decoded:
            raise GatewayDenied("secret_leak", sent=True)
        loaded = parse_upstream_chat(raw, content_type)
        _logger().info(
            "upstream status=%s kind=%s model=%s",
            status,
            self._response_kind,
            _public(loaded.get("model"), self.provider_key),
        )
        return loaded

    def _log_relay(
        self,
        *,
        project_id: str,
        status: int | None,
        requested_model: str,
        upstream_model: str,
        stream: bool,
        response_kind: str,
        incoming: int | None,
        outgoing: int | None,
        error: str,
    ) -> None:
        _logger().info(
            "relay project=%s status=%s requested_model=%s upstream_model=%s stream=%s kind=%s input_tokens=%s output_tokens=%s error=%s",
            _public(project_id, self.provider_key),
            "" if status is None else status,
            _public(requested_model, self.provider_key),
            _public(upstream_model, self.provider_key),
            stream,
            response_kind,
            "" if incoming is None else incoming,
            "" if outgoing is None else outgoing,
            _public(error, self.provider_key),
        )

    def _scope(self, token: str) -> dict[str, Any]:
        token_hash = sha256_text(token)
        with self.database.transaction() as conn:
            conn.execute(text("SELECT set_config('aether.token_hash', :token_hash, true)"), {"token_hash": token_hash})
            row = conn.execute(
                text(
                    "SELECT id::text AS id, project_id::text AS project_id, task_id::text AS task_id, scope, expires_at FROM capability_grants "
                    "WHERE token_hash = :token_hash AND capability_type = 'model' AND revoked_at IS NULL"
                ),
                {"token_hash": token_hash},
            ).mappings().first()
        if row is None or row["expires_at"] <= datetime.now(UTC) or row["task_id"] is None:
            raise GatewayDenied("token_rejected")
        scope = row["scope"]
        if isinstance(scope, str):
            scope = json.loads(scope)
        scope = dict(scope)
        if scope.get("task_id") != row["task_id"] or not scope.get("request_key") or not scope.get("attempt_id"):
            raise GatewayDenied("token_rejected")
        return {"id": row["id"], "project_id": row["project_id"], "task_id": row["task_id"], "scope": scope}

    def _admit(self, grant: dict[str, Any], *, alias: str, prompt: str, requested: Any) -> int:
        """Spend the token only for the exact request the budget ledger flipped to sending.

        A refused body leaves the token and the reservation untouched.
        """
        scope = grant["scope"]
        project_id = grant["project_id"]
        task_id = grant["task_id"]
        with self.database.transaction(project_id=project_id) as conn:
            live = conn.execute(
                text(
                    "SELECT 1 FROM capability_grants WHERE project_id = :project_id AND id = :id "
                    "AND revoked_at IS NULL AND expires_at > now() FOR UPDATE"
                ),
                {"project_id": project_id, "id": grant["id"]},
            ).first()
            if live is None:
                raise GatewayDenied("token_rejected")
            task = conn.execute(
                text("SELECT status, resume_state FROM tasks WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": task_id},
            ).mappings().first()
            if task is None or str(task["status"]) not in {"RUNNING", "VERIFYING"} or str(task["resume_state"] or "") in {"pause_requested", "cancel_requested"}:
                raise GatewayDenied("task_not_runnable")
            request = conn.execute(
                text(
                    "SELECT model_alias, usage_status, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": scope["request_key"]},
            ).mappings().first()
            if request is None or request["usage_status"] != "reserved":
                raise GatewayDenied("request_not_reserved")
            detail = request["usage_detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            detail = dict(detail or {})
            if detail.get("dispatch_state") != "sending" or detail.get("dispatch_holder") != scope["attempt_id"]:
                raise GatewayDenied("request_not_reserved")
            if str(request["model_alias"]) != alias:
                raise GatewayDenied("alias_denied")
            if detail.get("prompt_sha256") != sha256_text(prompt):
                raise GatewayDenied("prompt_mismatch")
            cap = int(detail.get("output_cap") or 0)
            try:
                max_tokens = cap if requested in (None, "") else int(requested)
            except (TypeError, ValueError):
                raise GatewayDenied("output_cap_exceeded") from None
            if cap < 1 or max_tokens < 1 or max_tokens > cap:
                raise GatewayDenied("output_cap_exceeded")
            conn.execute(
                text("UPDATE capability_grants SET revoked_at = now() WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": grant["id"]},
            )
        return max_tokens


def _object_detail(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _uuid_text(value: object) -> str:
    if not isinstance(value, str):
        raise GatewayDenied("token_rejected")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise GatewayDenied("token_rejected") from exc


def _fencing_token(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise GatewayDenied("token_rejected")
    fencing = int(value)
    if fencing > _MAX_FENCING:
        raise GatewayDenied("token_rejected")
    return fencing


def _coding_lease_held(
    conn: Connection,
    *,
    project_id: str,
    task_id: str,
    job_id: str,
    operation_id: str,
    fencing: int,
) -> bool:
    try:
        row = conn.execute(
            text(
                "SELECT 1 FROM coding_jobs AS j "
                "JOIN operations AS o ON o.project_id = j.project_id AND o.id = j.operation_id "
                "WHERE j.project_id = :project_id AND j.id = :job_id AND j.task_id = :task_id "
                "AND j.operation_id = :operation_id AND j.fencing_token = :fencing AND j.state = ANY(:states) "
                "AND o.lease_until > now() "
                "FOR UPDATE OF j"
            ),
            {
                "project_id": project_id,
                "job_id": job_id,
                "task_id": task_id,
                "operation_id": operation_id,
                "fencing": fencing,
                "states": list(_CODING_LEASE_STATES),
            },
        ).first()
    except DataError as exc:
        raise GatewayDenied("token_rejected") from exc
    return row is not None
