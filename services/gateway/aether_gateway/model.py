"""Job-scoped model calls. The provider key stays in this process."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import litellm
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database

litellm.suppress_debug_info = True


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


class GatewayDenied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelGateway:
    def __init__(self, database: Database, *, upstream_base: str, provider_key: str, timeout: float = 5) -> None:
        self.database = database
        self.upstream_base = upstream_base
        self.provider_key = provider_key
        self.timeout = timeout
        self.audit: list[str] = []

    def issue_token(self, *, owner_id: str, project_id: str, capabilities: list[str], aliases: list[str], egress: list[str] | None = None) -> str:
        token = secrets.token_urlsafe(32)
        scope = {"project_id": project_id, "capabilities": capabilities, "aliases": aliases, "egress": egress or []}
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO capability_grants (project_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
                    "VALUES (:project_id, 'job', :token_hash, 'model', CAST(:scope AS jsonb), :expires_at, 'v1')"
                ),
                {
                    "project_id": project_id,
                    "token_hash": sha256_text(token),
                    "scope": json.dumps(scope),
                    "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                },
            )
        return token

    def complete(self, token: str, body: dict[str, Any]) -> dict[str, Any]:
        scope = self._scope(token)
        alias = str(body.get("alias") or "")
        if alias not in scope["aliases"]:
            raise GatewayDenied("alias_denied")
        project_id = scope["project_id"]
        self.audit.append(f"model project={project_id} alias={alias}")
        call = {"max_tokens": int(body["max_tokens"])} if body.get("max_tokens") else {}
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
                    "usage_status": "unknown",
                    "input_tokens": None,
                    "output_tokens": None,
                    "actual_cost_micro_usd": None,
                    "cost_status": "unknown",
                }
            raise GatewayDenied("upstream_error") from exc
        output = _redact(response.choices[0].message.content or "", self.provider_key)
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
                "usage_status": "known",
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "actual_cost_micro_usd": cost,
                "cost_status": "known" if cost is not None else "unknown",
            }
        self.audit.append(f"model project={project_id} alias={alias} outcome={result['usage_status']}")
        encoded = json.dumps(result)
        if self.provider_key in encoded or any(self.provider_key in line for line in self.audit):
            raise GatewayDenied("secret_leak")
        return result

    def _scope(self, token: str) -> dict[str, Any]:
        token_hash = sha256_text(token)
        with self.database.transaction() as conn:
            conn.execute(text("SELECT set_config('aether.token_hash', :token_hash, true)"), {"token_hash": token_hash})
            row = conn.execute(
                text(
                    "SELECT scope, expires_at FROM capability_grants "
                    "WHERE token_hash = :token_hash AND revoked_at IS NULL"
                ),
                {"token_hash": token_hash},
            ).mappings().first()
        if row is None or row["expires_at"] <= datetime.now(UTC):
            raise GatewayDenied("token_rejected")
        scope = row["scope"]
        if isinstance(scope, str):
            scope = json.loads(scope)
        return dict(scope)
