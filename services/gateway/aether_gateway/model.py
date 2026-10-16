"""Job-scoped model calls. The provider key stays in this process."""

from __future__ import annotations

import json
import secrets
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
from aether_storage.db import Database

litellm.suppress_debug_info = True

# LiteLLM's response_cost is looked up in its bundled price table by the model
# name the upstream reports. It is a local estimate, not a provider bill.
COST_SOURCE = "local_price_estimate"
PRICING_VERSION = f"litellm-{version('litellm')}"
SCRIPTED_PROMPT_TOKENS = 8
SCRIPTED_COMPLETION_TOKENS = 4
CODING_CALL_OUTPUT_CAP = 256
CODING_MAX_MODEL_CALLS = 4
_CHAT_FIELDS = ("model", "messages", "tools", "tool_choice")
_CODING_LEASE_STATES = ("STARTING", "ACTIVE", "EXPORTING")
_MAX_FENCING = 9223372036854775807


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
    """Fields the coding relay may forward. timeout and any other key are dropped."""
    cleaned = {key: body[key] for key in _CHAT_FIELDS if key in body}
    if max_tokens is not None:
        cleaned["max_tokens"] = max_tokens
    return cleaned


def chat_prompt(body: dict[str, Any]) -> str:
    return json.dumps(accepted_chat_body(body), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def scripted_chat_completion(model: str) -> dict[str, Any]:
    """One finish call. No file edit, so the candidate files still decide the verdict."""
    prompt = SCRIPTED_PROMPT_TOKENS
    completion = SCRIPTED_COMPLETION_TOKENS
    return {
        "id": "chatcmpl-aether-scripted",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_finish",
                            "type": "function",
                            "function": {"name": "finish", "arguments": "{\"message\":\"done\"}"},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


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

    def __init__(self, code: str, *, sent: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.sent = sent


class ModelGateway:
    def __init__(self, database: Database, *, upstream_base: str, provider_key: str, timeout: float = 5) -> None:
        self.database = database
        self.upstream_base = upstream_base
        self.provider_key = provider_key
        self.timeout = timeout
        self.audit: list[str] = []

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
        one-time child grant. A repeat of that prompt is rejected, and another
        call past the reservation is rejected before the provider. The task row
        is not read or written.
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
        prompt = chat_prompt(body)
        max_tokens = self._spend_coding_ticket(grant, alias=alias, prompt=prompt)
        self.audit.append(f"relay project={grant['project_id']} model={model}")
        payload = accepted_chat_body(body, max_tokens=max_tokens)
        result = scripted_chat_completion(model or "coder") if not self.upstream_base else self._post_upstream(payload)
        self._add_measured_usage(grant, result)
        encoded = json.dumps(result)
        if self.provider_key and (self.provider_key in encoded or any(self.provider_key in line for line in self.audit)):
            raise GatewayDenied("secret_leak", sent=True)
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
            if reserved_tokens < 1 or measured + pending + CODING_CALL_OUTPUT_CAP > reserved_tokens:
                raise GatewayDenied("budget_exceeded")
            detail["pending_tokens"] = pending + CODING_CALL_OUTPUT_CAP
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
            child_detail = {
                "reserved_tokens": CODING_CALL_OUTPUT_CAP,
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
                detail["pending_tokens"] = max(0, _count(detail.get("pending_tokens")) - CODING_CALL_OUTPUT_CAP)
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

    def _add_measured_usage(self, session: dict[str, Any], result: dict[str, Any]) -> None:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return
        try:
            incoming = int(usage.get("prompt_tokens"))
            outgoing = int(usage.get("completion_tokens"))
        except (TypeError, ValueError):
            return
        if isinstance(usage.get("prompt_tokens"), bool) or isinstance(usage.get("completion_tokens"), bool):
            return
        if incoming < 0 or outgoing < 0:
            return
        project_id = str(session["project_id"])
        with self.database.transaction(project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id "
                    "AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {
                    "project_id": project_id,
                    "task_id": str(session["task_id"]),
                    "request_key": str(session["scope"]["request_key"]),
                },
            ).mappings().first()
            if row is None:
                return
            detail = row["usage_detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            detail = dict(detail or {})
            detail["measured_input_tokens"] = _count(detail.get("measured_input_tokens")) + incoming
            detail["measured_output_tokens"] = _count(detail.get("measured_output_tokens")) + outgoing
            detail["pending_tokens"] = max(0, _count(detail.get("pending_tokens")) - CODING_CALL_OUTPUT_CAP)
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {
                    "detail": json.dumps(detail),
                    "project_id": project_id,
                    "task_id": str(session["task_id"]),
                    "request_key": str(session["scope"]["request_key"]),
                },
            )

    def _post_upstream(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = accepted_chat_body(body, max_tokens=body.get("max_tokens") if isinstance(body.get("max_tokens"), int) else None)
        request = urllib.request.Request(
            _chat_url(self.upstream_base),
            data=json.dumps(payload).encode(),
            headers={"authorization": f"Bearer {self.provider_key}", "content-type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if self.provider_key and self.provider_key in detail:
                raise GatewayDenied("secret_leak", sent=True) from exc
            raise GatewayDenied("upstream_error", sent=True) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise GatewayDenied("upstream_error", sent=_is_timeout(exc)) from exc
        if self.provider_key and self.provider_key in text:
            raise GatewayDenied("secret_leak", sent=True)
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise GatewayDenied("upstream_error", sent=True)
        return loaded

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
