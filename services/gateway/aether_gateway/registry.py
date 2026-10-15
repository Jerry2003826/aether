"""Server-owned tool registry. MCP annotations are not consulted."""

from __future__ import annotations

from pathlib import Path

import yaml


class ToolDenied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_registry(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    tools = {item["id"]: item for item in document["tools"]}
    return {"tools": tools, "denied": set(document["actor_denied_tools"])}


def permissions_for_remote_tool(registry: dict, remote_tool: dict, *, tool_id: str) -> dict:
    """Permissions come from the local registry. Remote annotations are ignored."""
    if not isinstance(remote_tool, dict) or not remote_tool.get("name"):
        raise ToolDenied("tool_denied")
    if tool_id in registry["denied"] or tool_id not in registry["tools"]:
        raise ToolDenied("tool_denied")
    spec = registry["tools"][tool_id]
    approval = spec.get("approval")
    if approval is None:
        approval = "required" if spec["risk"] in {"R2", "R3"} else "none"
    return {
        "tool_id": tool_id,
        "risk": spec["risk"],
        "idempotency": spec["idempotency"],
        "required_capability": spec["required_capability"],
        "approval": approval,
    }


def admit_tool(registry: dict, scope: dict, tool_id: str, arguments: dict) -> dict:
    if tool_id in registry["denied"] or tool_id not in registry["tools"]:
        raise ToolDenied("tool_denied")
    spec = registry["tools"][tool_id]
    if spec["required_capability"] not in scope["capabilities"]:
        raise ToolDenied("capability_denied")
    if arguments.get("project_id") not in (None, scope["project_id"]):
        raise ToolDenied("project_mismatch")
    destination = arguments.get("destination")
    allowed = set(scope.get("egress") or [])
    if destination is not None and destination not in allowed:
        raise ToolDenied("egress_denied")
    return {"tool_id": tool_id, "project_id": scope["project_id"], "risk": spec["risk"], "accepted": True}
