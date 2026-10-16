"""Job-scoped model calls. The provider key stays in this process."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Any

import litellm
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.db import Database

litellm.suppress_debug_info = True

# LiteLLM's response_cost is looked up in its bundled price table by the model
# name the upstream reports. It is a local estimate, not a provider bill.
COST_SOURCE = "local_price_estimate"
PRICING_VERSION = f"litellm-{version('litellm')}"


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
    ) -> str:
        """A token for one booked request of one task attempt. It is spent by the first admitted call."""
        token = secrets.token_urlsafe(32)
        scope = {
            "project_id": project_id,
            "task_id": task_id,
            "request_key": request_key,
            "attempt_id": attempt_id,
            "capabilities": capabilities,
            "aliases": aliases,
            "egress": egress or [],
        }
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
                    "usage_status": "unknown",
                    "input_tokens": None,
                    "output_tokens": None,
                    "actual_cost_micro_usd": None,
                    "cost_status": "unknown",
                }
            raise GatewayDenied("upstream_error", sent=True) from exc
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
                "cost_status": "estimated" if cost is not None else "unknown",
                "cost_source": COST_SOURCE if cost is not None else "unreported",
                "pricing_version": PRICING_VERSION if cost is not None else None,
            }
        self.audit.append(f"model project={project_id} alias={alias} outcome={result['usage_status']}")
        encoded = json.dumps(result)
        if self.provider_key in encoded or any(self.provider_key in line for line in self.audit):
            raise GatewayDenied("secret_leak", sent=True)
        return result

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
