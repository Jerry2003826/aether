"""One Unix socket per coding job. The provider key never crosses this socket."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socketserver
import tempfile
import threading
from pathlib import Path

from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.db import Database
from aether_worker.loopback import LoopbackError, post_json

_MAX_BODY = 1_048_576
_MAX_LINE = 8192
_CHAT_PATHS = {"/v1/chat/completions", "/chat/completions"}


class CodingModelProxy:
    """Host-side OpenAI chat relay. The container presents only the job token."""

    def __init__(self, database: Database, *, job_token: str) -> None:
        self.database = database
        self.job_token = job_token
        self._closed = False
        self.directory = Path(tempfile.mkdtemp(prefix="aether-model-"))
        os.chmod(self.directory, 0o777)
        self.socket_path = self.directory / "model.sock"
        self._server = _UnixServer(str(self.socket_path), _Handler)
        self._server.proxy = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def respond(self, method: str, path: str, headers: dict[str, str], raw: bytes) -> tuple[int, dict]:
        if method != "POST" or path not in _CHAT_PATHS:
            return 404, {"error": "not_found"}
        expected = f"Bearer {self.job_token}"
        presented = headers.get("authorization", "")
        if not secrets.compare_digest(presented, expected):
            return 401, {"error": "unauthorized"}
        try:
            loaded = json.loads(raw.decode()) if raw else {}
        except (UnicodeError, json.JSONDecodeError):
            return 400, {"error": "body_rejected"}
        if not isinstance(loaded, dict):
            return 400, {"error": "body_rejected"}
        try:
            return 200, self._forward(loaded)
        except GatewayDenied as denied:
            return 409, {"error": denied.code}
        except (LoopbackError, OSError, TimeoutError, json.JSONDecodeError):
            return 502, {"error": "relay_failed"}

    def _forward(self, body: dict) -> dict:
        base = os.environ.get("AETHER_GATEWAY_BASE", "").rstrip("/")
        loopback = os.environ.get("AETHER_LOOPBACK_TOKEN", "")
        if base and loopback:
            payload = dict(body)
            payload["aether_job_token"] = self.job_token
            forwarded = post_json(f"{base}/v1/chat/completions", loopback, payload, timeout=30)
            if forwarded.get("denied") is True:
                raise GatewayDenied(str(forwarded.get("code") or "gateway_denied"), sent=bool(forwarded.get("sent")))
            return forwarded
        gateway = ModelGateway(
            self.database,
            upstream_base=os.environ.get("AETHER_LIVE_MODEL_BASE", "").strip(),
            provider_key=os.environ.get("AETHER_LIVE_MODEL_KEY", ""),
            timeout=25,
        )
        return gateway.relay_chat(self.job_token, body)


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    proxy: CodingModelProxy

    def server_bind(self) -> None:
        super().server_bind()
        os.chmod(self.server_address, 0o666)


class _Handler(socketserver.StreamRequestHandler):
    server: _UnixServer

    def handle(self) -> None:
        parsed = _read_http(self.rfile)
        if parsed is None:
            _write_json(self.wfile, 400, {"error": "body_rejected"})
            return
        method, path, headers, raw = parsed
        status, payload = self.server.proxy.respond(method, path, headers, raw)
        _write_json(self.wfile, status, payload)


def _read_http(reader) -> tuple[str, str, dict[str, str], bytes] | None:
    start = reader.readline(_MAX_LINE)
    if not start:
        return None
    parts = start.decode("iso-8859-1", "replace").strip().split(" ")
    if len(parts) < 2:
        return None
    headers: dict[str, str] = {}
    for _ in range(100):
        line = reader.readline(_MAX_LINE)
        if line in {b"\r\n", b"\n", b""}:
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
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict", 502: "Bad Gateway"}.get(
        status, "Error"
    )
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "content-type: application/json\r\n"
        f"content-length: {len(encoded)}\r\n"
        "connection: close\r\n"
        "\r\n"
    )
    writer.write(head.encode("ascii") + encoded)
    writer.flush()
