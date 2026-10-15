"""Bounded MCP stdio client. User text cannot add servers or grant tools."""

from __future__ import annotations

import json
import selectors
import subprocess
import sys
from typing import Any

from aether_domain.security import sha256_text


class McpError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise McpError("argv_rejected")
    if any(item in {"-c", "--command"} or any(token in item for token in ("|", ";", "`", "$(", "&&")) for item in argv):
        raise McpError("shell_rejected")
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)


def approved_servers(approved: set[str], user_text: str) -> list[str]:
    del user_text
    return sorted(approved)


def schema_hash(tools: list[dict[str, Any]]) -> str:
    body = [{key: value for key, value in tool.items() if key != "annotations"} for tool in tools]
    return sha256_text(json.dumps(body, sort_keys=True, separators=(",", ":")))


def admit(tool_name: str, tools: list[dict[str, Any]], *, allowed: set[str], approved_hash: str) -> None:
    if schema_hash(tools) != approved_hash:
        raise McpError("schema_quarantine")
    if tool_name not in allowed:
        raise McpError("tool_denied")


class McpClient:
    def __init__(self, process: subprocess.Popen[bytes], *, timeout: float = 2.0) -> None:
        self.process = process
        self.timeout = timeout
        self._next = 1

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise McpError("not_open")
        message = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params or {}}
        self._next += 1
        body = json.dumps(message).encode()
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        ready = selector.select(self.timeout)
        selector.close()
        if not ready:
            self.process.kill()
            return {"outcome": "unknown", "result": None}
        header = self.process.stdout.readline()
        if not header:
            return {"outcome": "unknown", "result": None}
        length = int(header.decode().split(":", 1)[1].strip())
        self.process.stdout.readline()
        payload = json.loads(self.process.stdout.read(length).decode())
        if "error" in payload:
            return {"outcome": "unknown", "result": None}
        return {"outcome": "known", "result": payload.get("result")}

    def call_tool(self, name: str, arguments: dict[str, Any], *, secret: str = "") -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if response["outcome"] != "known":
            return response
        encoded = json.dumps(response["result"])
        if secret and secret in encoded:
            encoded = encoded.replace(secret, "[REDACTED]")
            response["result"] = json.loads(encoded)
            if secret in json.dumps(response["result"]):
                raise McpError("secret_leak")
        return response


def fixture_argv(mode: str = "echo") -> list[str]:
    return [sys.executable, "tests/fixtures/mcp_fixture.py", mode]
