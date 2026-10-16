"""Run an approved tool through a reviewed MCP connector and the operation ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_gateway.mcp_client import McpClient, McpError, admit, spawn
from aether_gateway.registry import ToolDenied, admit_tool, load_registry, permissions_for_remote_tool
from aether_storage.db import Database
from aether_storage.operations import OperationError, OperationLedger

REGISTRY_ENV = "AETHER_TOOL_REGISTRY"
_ADMISSION_LIMIT = 256
_ADMISSION_DENIALS: dict[tuple[str, str], str] = {}


def registry_path() -> Path:
    raw = os.environ.get(REGISTRY_ENV, "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[3] / "config" / "tools.example.yaml"


def dispatch_approved_tools(database: Database, *, owner_id: str, project_id: str) -> None:
    """Dispatch each unused approval. One failure does not stop the next approval."""
    registry = load_registry(registry_path())
    ledger = OperationLedger(database)
    for approval in _approvals(database, owner_id=owner_id, project_id=project_id):
        try:
            _dispatch_one(database, ledger, registry, owner_id=owner_id, project_id=project_id, approval=approval)
        except (McpError, ToolDenied, OperationError, OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"aether-tool-failed project={project_id} approval={approval.get('id')} error={type(exc).__name__}", flush=True)


def _dispatch_one(
    database: Database,
    ledger: OperationLedger,
    registry: dict[str, Any],
    *,
    owner_id: str,
    project_id: str,
    approval: dict[str, Any],
) -> None:
    payload = _object(approval.get("requested_payload"))
    if sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":"))) != str(approval["intent_digest"]):
        return
    tool_id = payload.get("tool_id")
    spec = registry["tools"].get(tool_id) if isinstance(tool_id, str) else None
    arguments = payload.get("arguments")
    if spec is None or payload.get("risk") != spec["risk"] or not isinstance(arguments, dict):
        return
    if not _intent_complete(payload):
        return
    for connector in _connectors(database, owner_id=owner_id, project_id=project_id):
        binding = _binding(connector["manifest"], str(tool_id))
        if binding is None:
            continue
        denial_key = (str(approval["id"]), str(connector["manifest_sha256"]))
        if denial_key in _ADMISSION_DENIALS:
            continue
        if _dispatch_binding(
            database,
            ledger,
            registry,
            spec,
            owner_id=owner_id,
            project_id=project_id,
            approval=approval,
            payload=payload,
            arguments=arguments,
            connector=connector,
            binding=binding,
            denial_key=denial_key,
        ):
            return


def _dispatch_binding(
    database: Database,
    ledger: OperationLedger,
    registry: dict[str, Any],
    spec: dict[str, Any],
    *,
    owner_id: str,
    project_id: str,
    approval: dict[str, Any],
    payload: dict[str, Any],
    arguments: dict[str, Any],
    connector: dict[str, Any],
    binding: dict[str, str],
    denial_key: tuple[str, str],
) -> bool:
    grant = _grant(database, owner_id=owner_id, project_id=project_id, task_id=str(approval["task_id"]), capability=str(spec["required_capability"]))
    if grant is None:
        return False
    try:
        argv = spawn(list(connector["argv"]))
    except McpError as exc:
        _remember_denial(denial_key, exc.code)
        return False
    try:
        outcome = _call_and_dispatch(
            ledger,
            registry,
            spec,
            argv,
            binding,
            owner_id=owner_id,
            project_id=project_id,
            approval=approval,
            payload=payload,
            arguments=arguments,
            grant=grant,
        )
    except McpError as exc:
        if exc.code in {"schema_quarantine", "tool_denied", "argv_rejected", "shell_rejected"}:
            _remember_denial(denial_key, exc.code)
        return False
    except ToolDenied as exc:
        _remember_denial(denial_key, exc.code)
        return False
    if outcome.get("state") == "UNKNOWN" and spec["idempotency"] != "non_idempotent":
        _reconcile(ledger, argv, binding, owner_id=owner_id, project_id=project_id, operation_id=str(outcome["operation_id"]), arguments=arguments)
    return True


def _call_and_dispatch(
    ledger: OperationLedger,
    registry: dict[str, Any],
    spec: dict[str, Any],
    argv: list[str],
    binding: dict[str, str],
    *,
    owner_id: str,
    project_id: str,
    approval: dict[str, Any],
    payload: dict[str, Any],
    arguments: dict[str, Any],
    grant: dict[str, Any],
) -> dict[str, Any]:
    mcp_name = binding["mcp_name"]
    with McpClient(argv, timeout=5) as client:
        if _admitted_tool(client, registry, spec, binding, project_id=project_id, arguments=arguments, grant=grant) is None:
            raise McpError("connect_failed")

        def sender(intent: dict[str, Any]) -> dict[str, Any]:
            call_args = intent.get("arguments") if isinstance(intent.get("arguments"), dict) else arguments
            response = client.call_tool(mcp_name, call_args)
            if response.get("outcome") != "known" or not isinstance(response.get("result"), dict):
                raise TimeoutError("mcp_outcome_unknown")
            return response["result"]

        return ledger.dispatch(
            owner_id=owner_id,
            project_id=project_id,
            task_id=str(approval["task_id"]),
            approval_id=str(approval["id"]),
            intent=payload,
            capability_id=str(grant["id"]),
            idempotency=str(spec["idempotency"]),
            sender=sender,
        )


def _reconcile(
    ledger: OperationLedger,
    argv: list[str],
    binding: dict[str, str],
    *,
    owner_id: str,
    project_id: str,
    operation_id: str,
    arguments: dict[str, Any],
) -> None:
    def reader(stored: Any) -> dict[str, Any] | None:
        payload = stored if isinstance(stored, dict) else arguments
        try:
            with McpClient(argv, timeout=5) as client:
                listed = client.request("tools/list")
                tools = listed.get("result", {}).get("tools") if listed.get("outcome") == "known" else None
                if not isinstance(tools, list):
                    return None
                admit(binding["mcp_name"], tools, allowed={binding["mcp_name"]}, approved_hash=binding["approved_schema_hash"])
                response = client.call_tool(binding["mcp_name"], payload)
        except (McpError, ToolDenied, OSError, TimeoutError):
            return None
        if response.get("outcome") != "known" or not isinstance(response.get("result"), dict):
            return None
        return response["result"]

    ledger.reconcile(owner_id=owner_id, project_id=project_id, operation_id=operation_id, reader=reader)


def _admitted_tool(
    client: McpClient,
    registry: dict[str, Any],
    spec: dict[str, Any],
    binding: dict[str, str],
    *,
    project_id: str,
    arguments: dict[str, Any],
    grant: dict[str, Any],
) -> dict[str, Any] | None:
    listed = client.request("tools/list")
    if listed.get("outcome") != "known":
        return None
    tools = listed.get("result", {}).get("tools")
    if not isinstance(tools, list):
        return None
    admit(binding["mcp_name"], tools, allowed={binding["mcp_name"]}, approved_hash=binding["approved_schema_hash"])
    remote = next((tool for tool in tools if isinstance(tool, dict) and tool.get("name") == binding["mcp_name"]), None)
    if remote is None:
        raise McpError("tool_denied")
    permissions = permissions_for_remote_tool(registry, remote, tool_id=binding["tool_id"])
    if permissions["risk"] != spec["risk"] or permissions["idempotency"] != spec["idempotency"] or permissions["required_capability"] != spec["required_capability"]:
        raise ToolDenied("tool_denied")
    scope = grant.get("scope") if isinstance(grant.get("scope"), dict) else {}
    egress = scope.get("egress") if isinstance(scope.get("egress"), list) else []
    admit_tool(
        registry,
        {"project_id": project_id, "capabilities": [str(spec["required_capability"])], "egress": [str(item) for item in egress]},
        str(permissions["tool_id"]),
        arguments,
    )
    return remote


def _remember_denial(key: tuple[str, str], code: str) -> None:
    if len(_ADMISSION_DENIALS) >= _ADMISSION_LIMIT:
        _ADMISSION_DENIALS.clear()
    _ADMISSION_DENIALS[key] = code


def _intent_complete(payload: dict[str, Any]) -> bool:
    required = ("intent_key", "tool_id", "resource_id", "resource_version", "risk")
    return all(isinstance(payload.get(name), str) and payload.get(name) for name in required)


def _binding(manifest: dict[str, Any], tool_id: str) -> dict[str, str] | None:
    raw = manifest.get("bindings")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("tool_id") != tool_id:
            continue
        mcp_name = item.get("mcp_name")
        approved = item.get("approved_schema_hash")
        if isinstance(mcp_name, str) and mcp_name and isinstance(approved, str) and approved:
            return {"tool_id": tool_id, "mcp_name": mcp_name, "approved_schema_hash": approved}
    return None


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    return {}


def _approvals(database: Database, *, owner_id: str, project_id: str) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT id::text AS id, task_id::text AS task_id, intent_digest, requested_payload "
                "FROM approvals WHERE project_id = :project_id AND owner_id = :owner_id "
                "AND decision = 'approved' AND consumed_by_operation_id IS NULL AND expires_at > now() "
                "ORDER BY created_at, id"
            ),
            {"project_id": project_id, "owner_id": owner_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def _connectors(database: Database, *, owner_id: str, project_id: str) -> list[dict[str, Any]]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT manifest_sha256, reviewed_manifest FROM connector_configs "
                "WHERE project_id = :project_id AND status = 'reviewed' ORDER BY created_at, id"
            ),
            {"project_id": project_id},
        ).mappings().all()
    found: list[dict[str, Any]] = []
    for row in rows:
        manifest = _object(row["reviewed_manifest"])
        argv = manifest.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
            continue
        found.append({"manifest_sha256": str(row["manifest_sha256"]), "manifest": manifest, "argv": argv})
    return found


def _grant(database: Database, *, owner_id: str, project_id: str, task_id: str, capability: str) -> dict[str, Any] | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        row = conn.execute(
            text(
                "SELECT id::text AS id, scope FROM capability_grants "
                "WHERE project_id = :project_id AND task_id = :task_id AND capability_type = :capability "
                "AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC LIMIT 1"
            ),
            {"project_id": project_id, "task_id": task_id, "capability": capability},
        ).mappings().first()
    if row is None:
        return None
    return {"id": str(row["id"]), "scope": _object(row["scope"])}
