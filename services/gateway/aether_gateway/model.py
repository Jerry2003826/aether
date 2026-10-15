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


class GatewayDenied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelGateway:
    def __init__(self, database: Database, *, upstream_base: str, provider_key: str) -> None:
        self.database = database
        self.upstream_base = upstream_base
        self.provider_key = provider_key
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
        response = litellm.completion(
            model="openai/local-fake",
            api_base=self.upstream_base,
            api_key=self.provider_key,
            messages=[{"role": "user", "content": str(body.get("prompt") or "")}],
            timeout=5,
        )
        output = response.choices[0].message.content
        usage = response.usage
        result = {
            "project_id": project_id,
            "task_id": scope.get("task_id"),
            "output": output,
            "usage_status": "known",
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
        }
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
