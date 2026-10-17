"""One Unix socket per coding job. The provider key never crosses this socket."""

from __future__ import annotations

import io
import json
import logging
import math
import os
import secrets
import shutil
import socket
import socketserver
import stat
import struct
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.db import Database
from aether_worker.loopback import LoopbackError, post_json

_MAX_BODY = 1_048_576
_MAX_LINE = 8192
_CHAT_PATHS = {"/v1/chat/completions", "/chat/completions"}
# The sandbox entry posts here (with the job token) to end the model channel before grading.
# openhands_sandbox_entry.py runs in the sandbox image and carries the same literal.
REVOKE_PATH = "/aether/revoke"
_SANDBOX_UID = 65534
_ACL_USER_OBJ = 0x01
_ACL_USER = 0x02
_ACL_GROUP_OBJ = 0x04
_ACL_MASK = 0x10
_ACL_OTHER = 0x20
_ACL_UNDEFINED_ID = 0xFFFFFFFF
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LOG = logging.getLogger("aether.worker.coding_proxy")
# A sandbox client gets this long to deliver one whole request (and to read the reply), and may
# hold at most this many connections at once. Excess connections are refused with 503 at accept
# time, without a thread. OpenHands sends one request at a time, so the defaults leave headroom.
READ_TIMEOUT_ENV = "AETHER_CODING_PROXY_READ_TIMEOUT_SECONDS"
MAX_CONNECTIONS_ENV = "AETHER_CODING_PROXY_MAX_CONNECTIONS"
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONNECTIONS = 8
# After the sandbox exits, a call may still be on its way through this proxy. The job waits
# this long for it before settling; the default outlasts one forwarded call (30 s).
DRAIN_ENV = "AETHER_CODING_PROXY_DRAIN_SECONDS"
DEFAULT_DRAIN_SECONDS = 35.0


def proxy_limits() -> tuple[float, int]:
    """(read timeout seconds, max concurrent connections) from the environment; bad values use the defaults."""
    try:
        timeout = float(os.environ.get(READ_TIMEOUT_ENV, ""))
    except ValueError:
        timeout = DEFAULT_READ_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = DEFAULT_READ_TIMEOUT_SECONDS
    try:
        limit = int(os.environ.get(MAX_CONNECTIONS_ENV, ""))
    except ValueError:
        limit = DEFAULT_MAX_CONNECTIONS
    if limit < 1:
        limit = DEFAULT_MAX_CONNECTIONS
    return timeout, limit


def drain_seconds() -> float:
    """AETHER_CODING_PROXY_DRAIN_SECONDS, or the default when unset or not a finite positive number."""
    try:
        value = float(os.environ.get(DRAIN_ENV, ""))
    except ValueError:
        return DEFAULT_DRAIN_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_DRAIN_SECONDS
    return value


class CodingModelProxy:
    """Host-side OpenAI chat relay. The container presents only the job token."""

    def __init__(self, database: Database, *, job_token: str) -> None:
        self.database = database
        self.job_token = job_token
        self._closed = False
        self._listening = True
        self._listen_lock = threading.Lock()
        self._revoke_lock = threading.Lock()
        self._revoked_by: str | None = None
        self._forwarding = 0
        self._stopped = threading.Event()
        self.directory = Path(tempfile.mkdtemp(prefix="aether-model-"))
        try:
            _grant_directory(self.directory)
            self.socket_path = self.directory / "model.sock"
            self._server = _UnixServer(str(self.socket_path), _Handler)
            self._server.read_timeout, limit = proxy_limits()
            self._server.slots = threading.BoundedSemaphore(limit)
            self._server.changed = threading.Condition()
        except OSError:
            shutil.rmtree(self.directory, ignore_errors=True)
            raise
        self._server.proxy = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def active_connections(self) -> int:
        """Connections currently holding a slot (read-only, for monitoring and tests)."""
        with self._server.changed:
            return self._server.active

    @property
    def peak_connections(self) -> int:
        """Most connections that ever held a slot at once."""
        with self._server.changed:
            return self._server.peak

    def wait_until_active(self, count: int, *, timeout: float) -> bool:
        """Block until exactly `count` connections hold a slot, or `timeout` passes."""
        with self._server.changed:
            return self._server.changed.wait_for(lambda: self._server.active == count, timeout=timeout)

    def drain(
        self,
        *,
        timeout: float | None = None,
        heartbeat: Callable[[], None] | None = None,
        heartbeat_seconds: float | None = None,
    ) -> int:
        """Stop accepting connections, then wait for the ones in flight. Returns how many are left.

        The listener is closed first, so nothing new starts while we wait. Handler threads
        are not interrupted: a call that is still forwarding keeps going and may still answer.
        0 means every in-flight call finished (its usage is recorded) within the timeout.

        With a heartbeat, the wait is cut into slices of at most heartbeat_seconds and the
        heartbeat runs between slices, so a caller can keep a lease alive for the whole wait.
        An exception from the heartbeat ends the drain and propagates.
        """
        self._stop_listening()
        total = drain_seconds() if timeout is None else timeout
        if heartbeat is None or heartbeat_seconds is None or heartbeat_seconds <= 0:
            self.wait_until_active(0, timeout=total)
            return self.active_connections
        deadline = time.monotonic() + total
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.wait_until_active(0, timeout=min(heartbeat_seconds, remaining)):
                break
            if deadline - time.monotonic() <= 0:
                break
            heartbeat()
        return self.active_connections

    @property
    def forwarding_calls(self) -> int:
        """Model calls registered (under the revoke lock) and still being forwarded."""
        with self._revoke_lock:
            return self._forwarding

    @property
    def revoked(self) -> bool:
        return self._revoked_by is not None

    @property
    def revoked_in_band(self) -> bool:
        """True when the first revoke was an authenticated POST to REVOKE_PATH over the socket (the
        sandbox entry's), False when nothing revoked yet or the worker's revoke() came first.

        The proxy does not know the container's lifetime. coding_jobs reads this once, after
        run_coding_sandbox has returned (the container has exited and its logs were read) and
        before its own revoke() in the drain, so True there means the in-band revoke happened
        while the container ran. Later revokes never change it."""
        return self._revoked_by == "sandbox"

    def wait_until_revoked(self, *, timeout: float) -> bool:
        """Block until a revoke has removed the socket and closed the listener, or `timeout` passes."""
        return self.revoked and self._stopped.wait(timeout)

    def revoke(self) -> None:
        """Host-side backstop: end the model channel from the worker. Same effect as the sandbox's revoke."""
        self._revoke("worker")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_listening()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _stop_listening(self) -> None:
        # Exactly once, whoever gets here first (drain, close or a revoke on any thread).
        with self._listen_lock:
            if self._listening:
                self._listening = False
                self._server.shutdown()
                self._server.server_close()
        self._stopped.set()

    def _revoke(self, by: str) -> None:
        """End the model channel for the rest of the job: no more model calls, and the socket is gone.

        The sandbox asks for this (with the job token) once the agent phase ends and before it runs
        candidate code for grading, so that code cannot reach the paid model; the worker calls it too
        when the container is done. The socket file is unlinked here on the host (the container
        cannot remove it), so a later connect fails with ENOENT, and the listener is closed.

        Idempotent and safe under concurrent calls: the first caller marks the channel revoked and
        unlinks the socket; the listener closes exactly once (shared with drain/close). Calls already
        being forwarded are not interrupted: they answer and their usage is recorded as usual, and
        drain still waits for them; only calls that start after the revoke are refused. Closing the
        listener from a handler thread is safe (serve_forever runs on its own thread) and the
        accepted connection still gets its answer.
        """
        with self._revoke_lock:
            if self._revoked_by is not None:
                return
            self._revoked_by = by
            self.socket_path.unlink(missing_ok=True)
        self._stop_listening()

    def respond(self, method: str, path: str, headers: dict[str, str], raw: bytes) -> tuple[int, dict]:
        if method != "POST" or (path not in _CHAT_PATHS and path != REVOKE_PATH):
            return 404, {"error": "not_found"}
        expected = f"Bearer {self.job_token}"
        presented = headers.get("authorization", "")
        # Constant-time; a non-ASCII header can never match (and str compare_digest rejects it).
        if not presented.isascii() or not secrets.compare_digest(presented, expected):
            return 401, {"error": "unauthorized"}
        if path == REVOKE_PATH:
            self._revoke("sandbox")
            return 200, {"revoked": True}
        try:
            loaded = json.loads(raw.decode()) if raw else {}
        except (UnicodeError, json.JSONDecodeError):
            return 400, {"error": "body_rejected"}
        if not isinstance(loaded, dict):
            return 400, {"error": "body_rejected"}
        # Checked under the revoke lock right before forwarding: once revoke() has returned, no call
        # starts forwarding. A call that got here first is in flight (its connection holds a slot),
        # so drain waits for it and its usage settles normally.
        with self._revoke_lock:
            if self._revoked_by is not None:
                return 401, {"error": "revoked"}
            self._forwarding += 1
        try:
            return 200, self._forward(loaded)
        except GatewayDenied as denied:
            return 409, {"error": denied.code}
        except (LoopbackError, OSError, TimeoutError, json.JSONDecodeError):
            return 502, {"error": "relay_failed"}
        finally:
            with self._revoke_lock:
                self._forwarding -= 1

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
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS
    slots: threading.BoundedSemaphore
    changed: threading.Condition
    active = 0
    peak = 0

    def _count(self, delta: int) -> None:
        with self.changed:
            self.active += delta
            self.peak = max(self.peak, self.active)
            self.changed.notify_all()

    def _release(self) -> None:
        self._count(-1)
        self.slots.release()

    def process_request(self, request, client_address) -> None:
        """Start a handler thread only while a connection slot is free; otherwise refuse at once."""
        if not self.slots.acquire(blocking=False):
            _logger().info("coding_proxy status=503 error=too_many_connections")
            _refuse(request)
            self.shutdown_request(request)
            return
        self._count(1)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()

    def server_bind(self) -> None:
        super().server_bind()
        try:
            _grant_socket(self.server_address)
        except OSError:
            self.server_close()
            raise


class _DeadlineReader(io.RawIOBase):
    """Socket reads that share one deadline, so trickling bytes cannot keep a connection open."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request_deadline")
        self._sock.settimeout(remaining)
        return self._sock.recv_into(buffer)


class _Handler(socketserver.StreamRequestHandler):
    server: _UnixServer

    def setup(self) -> None:
        super().setup()
        deadline = time.monotonic() + self.server.read_timeout
        self.rfile = io.BufferedReader(_DeadlineReader(self.connection, deadline))

    def handle(self) -> None:
        try:
            parsed = _read_http(self.rfile)
        except (TimeoutError, OSError):
            _logger().info("coding_proxy status=408 error=read_timeout")
            return
        # The reply gets the same bound, so a client that stops reading cannot pin the thread.
        self.connection.settimeout(self.server.read_timeout)
        if parsed is None:
            _write_json(self.wfile, 400, {"error": "body_rejected"})
            return
        method, path, headers, raw = parsed
        status, payload = self.server.proxy.respond(method, path, headers, raw)
        error = payload.get("error") if status >= 400 else ""
        try:
            _write_json(self.wfile, status, payload)
        except (TimeoutError, OSError):
            # The client stopped reading. One line, no socketserver traceback.
            _logger().info("coding_proxy status=%s error=write_timeout", status)
            return
        _logger().info("coding_proxy status=%s error=%s", status, error if isinstance(error, str) else "")


def _refuse(request: socket.socket) -> None:
    encoded = json.dumps({"error": "too_many_connections"}).encode()
    head = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        "content-type: application/json\r\n"
        f"content-length: {len(encoded)}\r\n"
        "connection: close\r\n"
        "\r\n"
    )
    # One non-blocking send on the accept thread: a peer with a full buffer just gets nothing.
    try:
        request.setblocking(False)
        request.send(head.encode("ascii") + encoded)
    except OSError:
        pass


def _posix_acl(owner: int, named: int) -> bytes:
    entries = (
        (_ACL_USER_OBJ, owner, _ACL_UNDEFINED_ID),
        (_ACL_USER, named, _SANDBOX_UID),
        (_ACL_GROUP_OBJ, 0, _ACL_UNDEFINED_ID),
        (_ACL_MASK, named, _ACL_UNDEFINED_ID),
        (_ACL_OTHER, 0, _ACL_UNDEFINED_ID),
    )
    header = struct.pack("<I", 2)
    body = b"".join(struct.pack("<HHI", tag, perm, ident) for tag, perm, ident in entries)
    return header + body


def _grant_directory(path: Path) -> None:
    """Owner mode plus traverse for uid 65534. Never leave the directory world-writable."""
    fd = os.open(path, _DIR_FLAGS)
    try:
        os.fchmod(fd, 0o700)
        os.setxattr(f"/proc/self/fd/{fd}", "system.posix_acl_access", _posix_acl(0o7, 0o5), follow_symlinks=True)
    finally:
        os.close(fd)


def _grant_socket(path: str | os.PathLike[str]) -> None:
    """Owner mode plus connect for uid 65534. An ACL failure does not fall back to 0o666."""
    if not stat.S_ISSOCK(os.lstat(path).st_mode):
        raise OSError("socket_replaced")
    os.chmod(path, 0o600)
    os.setxattr(path, "system.posix_acl_access", _posix_acl(0o6, 0o6), follow_symlinks=False)


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


def _logger() -> logging.Logger:
    _LOG.setLevel(logging.INFO)
    if not _LOG.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        _LOG.addHandler(handler)
    return _LOG


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
