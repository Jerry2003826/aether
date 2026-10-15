"""MCP client. Transport uses the official Python SDK; this module only decides what may run."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from concurrent.futures import Future
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

from aether_domain.security import sha256_text


class McpError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _validate_argv(argv: list[str]) -> list[str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise McpError("argv_rejected")
    if any(item in {"-c", "--command"} or any(token in item for token in ("|", ";", "`", "$(", "&&")) for item in argv):
        raise McpError("shell_rejected")
    return argv


def spawn(argv: list[str]) -> list[str]:
    """Reject shell strings. The official SDK receives the same argv list, not a shell line."""
    return _validate_argv(argv)


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
    def __init__(self, argv: list[str], *, timeout: float = 5.0) -> None:
        self.argv = _validate_argv(argv)
        self.timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._queue: asyncio.Queue[tuple[str, dict[str, Any], Future[dict[str, Any]]] | None] | None = None
        self._worker: Future[None] | None = None

    def __enter__(self) -> McpClient:
        self._thread.start()
        ready: Future[None] = Future()

        async def serve() -> None:
            self._queue = asyncio.Queue()
            parameters = StdioServerParameters(command=self.argv[0], args=self.argv[1:])
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=self.timeout) as session:
                    await session.initialize()
                    ready.set_result(None)
                    await self._serve(session)

        self._worker = asyncio.run_coroutine_threadsafe(serve(), self._loop)
        ready.result(timeout=self.timeout + 5)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._queue is not None:
            asyncio.run_coroutine_threadsafe(self._queue.put(None), self._loop).result(timeout=5)
        if self._worker is not None:
            self._worker.result(timeout=8)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._submit("request", {"method": method, "params": params or {}})

    def call_tool(self, name: str, arguments: dict[str, Any], *, secret: str = "", timeout: float | None = None) -> dict[str, Any]:
        response = self._submit("call", {"name": name, "arguments": arguments, "timeout": self.timeout if timeout is None else timeout})
        if response["outcome"] != "known":
            return response
        encoded = json.dumps(response["result"])
        if secret and secret in encoded:
            encoded = encoded.replace(secret, "[REDACTED]")
            response["result"] = json.loads(encoded)
            if secret in json.dumps(response["result"]):
                raise McpError("secret_leak")
        return response

    def _submit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        done: Future[dict[str, Any]] = Future()

        async def enqueue() -> None:
            if self._queue is None:
                done.set_result({"outcome": "unknown", "result": None})
                return
            await self._queue.put((kind, payload, done))

        asyncio.run_coroutine_threadsafe(enqueue(), self._loop).result(timeout=5)
        return done.result(timeout=self.timeout + 5)

    async def _serve(self, session: ClientSession) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:
                return
            kind, payload, done = item
            try:
                if kind == "request":
                    result = await self._request(session, str(payload["method"]), dict(payload["params"]))
                else:
                    result = await self._call_tool(session, str(payload["name"]), dict(payload["arguments"]), float(payload["timeout"]))
            except MCPError:
                result = {"outcome": "unknown", "result": None}
            if not done.done():
                done.set_result(result)

    async def _request(self, session: ClientSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"outcome": "known", "result": {"protocolVersion": str(session.protocol_version)}}
        if method == "tools/list":
            listed = await session.list_tools()
            tools = [tool.model_dump(by_alias=True, mode="json") for tool in listed.tools]
            return {"outcome": "known", "result": {"tools": tools}}
        if method == "tools/call":
            return await self._call_tool(session, str(params.get("name") or ""), dict(params.get("arguments") or {}), self.timeout)
        return {"outcome": "unknown", "result": None}

    async def _call_tool(self, session: ClientSession, name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            result = await session.call_tool(name, arguments, read_timeout_seconds=timeout)
        except MCPError as exc:
            if "timed out" in str(exc).lower():
                return {"outcome": "unknown", "result": None}
            return {"outcome": "unknown", "result": None}
        return {"outcome": "known", "result": result.model_dump(by_alias=True, mode="json")}


def fixture_argv(mode: str = "echo") -> list[str]:
    return [sys.executable, "tests/fixtures/mcp_fixture.py", mode]
