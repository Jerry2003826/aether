"""Loopback HTTP for a privileged role. GET /health is open. POST requires the bearer token.

The listener is a plain TCP server on 127.0.0.1. It does not use http.server.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from collections.abc import Callable

Health = Callable[[], dict]
Post = Callable[[str, dict], tuple[int, dict]]
_MAX_BODY = 1_048_576
_MAX_LINE = 8192
ERROR_KEY = "error"
BODY_REJECTED = "body_rejected"
_STATUS_TEXT = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 500: "Error"}


class RoleHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, token: str, health_payload: Health, post_payload: Post) -> None:
        self.token = token
        self.health_payload = health_payload
        self.post_payload = post_payload
        self._stopped = threading.Event()
        self._stopped_ack = threading.Event()
        super().__init__(("127.0.0.1", 0), _RoleConnection)
        self.timeout = 0.2

    def shutdown(self) -> None:
        self._stopped.set()
        self._stopped_ack.wait()

    def serve_until_stopped(self) -> None:
        try:
            while not self._stopped.is_set():
                self.handle_request()
        finally:
            self._stopped_ack.set()


class _RoleConnection(socketserver.StreamRequestHandler):
    server: RoleHTTPServer

    def handle(self) -> None:
        parsed = _read_request(self.rfile)
        if parsed is None:
            _write_json(self.wfile, 400, {ERROR_KEY: BODY_REJECTED})
            return
        method, path, headers, raw = parsed
        if method == "GET":
            _write_json(self.wfile, *_get(self.server, path))
            return
        if method == "POST":
            _write_json(self.wfile, *_post(self.server, path, headers, raw))
            return
        _write_json(self.wfile, 404, {ERROR_KEY: "not_found"})


def serve_role(token: str, health_payload: Health, post_payload: Post) -> None:
    server = RoleHTTPServer(token, health_payload, post_payload)
    port = int(server.server_address[1])
    print(f"aether-role-ready port={port}", flush=True)
    _detach_stdio()
    server.serve_until_stopped()


def _get(server: RoleHTTPServer, path: str) -> tuple[int, dict]:
    if path != "/health":
        return 404, {ERROR_KEY: "not_found"}
    try:
        return 200, server.health_payload()
    except Exception:
        return 500, {ERROR_KEY: "health_failed"}


def _post(server: RoleHTTPServer, path: str, headers: dict[str, str], raw: bytes) -> tuple[int, dict]:
    if headers.get("authorization") != f"Bearer {server.token}":
        return 401, {ERROR_KEY: "unauthorized"}
    body = _json_body(raw)
    if body is None:
        return 400, {ERROR_KEY: BODY_REJECTED}
    try:
        return server.post_payload(path, body)
    except Exception:
        return 500, {ERROR_KEY: "role_failed"}


def _json_body(raw: bytes) -> dict | None:
    try:
        loaded = json.loads(raw.decode()) if raw else {}
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _read_request(reader) -> tuple[str, str, dict[str, str], bytes] | None:
    start = reader.readline(_MAX_LINE)
    if not start:
        return None
    parts = start.decode("iso-8859-1", "replace").strip().split(" ")
    if len(parts) < 2:
        return None
    headers: dict[str, str] = {}
    for _ in range(100):
        line = reader.readline(_MAX_LINE)
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode("iso-8859-1", "replace")
        if ":" not in decoded:
            return None
        name, value = decoded.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    else:
        return None
    try:
        length = int(headers.get("content-length") or "0")
    except ValueError:
        return None
    if length < 0 or length > _MAX_BODY:
        return None
    raw = _read_exact(reader, length)
    if raw is None:
        return None
    return parts[0].upper(), parts[1].split("?", 1)[0], headers, raw


def _read_exact(reader, length: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        block = reader.read(remaining)
        if not block:
            return None
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _write_json(writer, status: int, payload: dict) -> None:
    encoded = json.dumps(payload).encode()
    reason = _STATUS_TEXT.get(status, "Error")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "content-type: application/json\r\n"
        f"content-length: {len(encoded)}\r\n"
        "connection: close\r\n"
        "\r\n"
    )
    writer.write(head.encode("ascii") + encoded)
    writer.flush()


def _detach_stdio() -> None:
    """Drop the inherited pipes so the parent can exec without leaving a full pipe."""
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    finally:
        os.close(devnull)
