"""The coding model proxy bounds how long a sandbox client may hold a connection and how many it may hold."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest

from aether_storage.db import Database
from aether_worker.coding_model_proxy import CodingModelProxy

_JOB = "job-token-proxy-limits"
_LOOP = "loopback-proxy-limits"
_TIMEOUT = 0.4
_LIMIT = 2


class _Gateway(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or "0"))
        encoded = json.dumps({"id": "ok", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def proxy(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AETHER_GATEWAY_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", _LOOP)
    monkeypatch.delenv("AETHER_LIVE_MODEL_KEY", raising=False)
    monkeypatch.setenv("AETHER_CODING_PROXY_READ_TIMEOUT_SECONDS", str(_TIMEOUT))
    monkeypatch.setenv("AETHER_CODING_PROXY_MAX_CONNECTIONS", str(_LIMIT))
    made = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    try:
        yield made
    finally:
        made.close()
        server.shutdown()
        server.server_close()


def _connect(proxy: CodingModelProxy) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(proxy.socket_path))
    return sock


def _closed_within(sock: socket.socket, seconds: float) -> float | None:
    """Seconds until the server ends the connection, or None if it is still open after `seconds`."""
    started = time.monotonic()
    sock.settimeout(seconds)
    try:
        while sock.recv(4096):
            pass
    except TimeoutError:
        return None
    except OSError:
        pass
    return time.monotonic() - started


def _ask(proxy: CodingModelProxy) -> int:
    raw = json.dumps({"model": "coder", "messages": []}).encode()
    request = (
        "POST /v1/chat/completions HTTP/1.1\r\n"
        f"authorization: Bearer {_JOB}\r\n"
        f"content-length: {len(raw)}\r\n"
        "\r\n"
    ).encode("ascii") + raw
    sock = _connect(proxy)
    try:
        sock.settimeout(5)
        sock.sendall(request)
        chunks: list[bytes] = []
        while block := sock.recv(65536):
            chunks.append(block)
    finally:
        sock.close()
    return int(b"".join(chunks).split(b" ", 2)[1])


def test_idle_client_is_disconnected_after_the_read_timeout(proxy) -> None:
    sock = _connect(proxy)
    try:
        elapsed = _closed_within(sock, 3.0)
    finally:
        sock.close()
    assert elapsed is not None, "idle connection was never closed"
    assert elapsed < _TIMEOUT + 1.0


def test_trickling_client_is_cut_off_by_the_request_deadline(proxy) -> None:
    sock = _connect(proxy)
    stop = time.monotonic() + 3.0
    cut = False
    try:
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n")
        while time.monotonic() < stop:
            try:
                sock.sendall(b"x")
            except OSError:
                cut = True
                break
            sock.settimeout(0.1)
            try:
                if sock.recv(4096) == b"":
                    cut = True
                    break
            except TimeoutError:
                pass
    finally:
        sock.close()
    assert cut, "a client that never finishes its request kept the connection for 3s"


def _status_line(sock: socket.socket, seconds: float) -> bytes:
    sock.settimeout(seconds)
    chunks: list[bytes] = []
    try:
        while block := sock.recv(4096):
            chunks.append(block)
    except (TimeoutError, OSError):
        pass
    return b"".join(chunks).split(b"\r\n", 1)[0]


def test_idle_connections_beyond_the_limit_do_not_pile_up_threads(proxy) -> None:
    idle = [_connect(proxy) for _ in range(_LIMIT)]
    extra: list[socket.socket] = []
    try:
        assert proxy.wait_until_active(_LIMIT, timeout=2.0)
        extra = [_connect(proxy) for _ in range(6)]
        # Connections over the limit are refused at once with 503, without a handler.
        for sock in extra:
            assert _status_line(sock, 2.0) == b"HTTP/1.1 503 Service Unavailable"
        assert proxy.active_connections == _LIMIT
        assert proxy.peak_connections == _LIMIT
        # The idle ones are dropped by the read deadline, which frees every slot.
        assert proxy.wait_until_active(0, timeout=_TIMEOUT + 2.0)
        assert _ask(proxy) == 200
        assert _ask(proxy) == 200
        assert proxy.wait_until_active(0, timeout=2.0)
        assert proxy.peak_connections == _LIMIT
    finally:
        for sock in idle + extra:
            sock.close()


def test_refusal_never_blocks_the_accept_thread() -> None:
    from aether_worker import coding_model_proxy

    class Full:
        """A peer whose receive buffer is full: any blocking send would hang."""

        def __init__(self) -> None:
            self.blocking = True
            self.calls: list[str] = []

        def setblocking(self, flag: bool) -> None:
            self.blocking = flag
            self.calls.append(f"setblocking:{flag}")

        def settimeout(self, value) -> None:
            self.blocking = value is None
            self.calls.append(f"settimeout:{value}")

        def send(self, _data: bytes) -> int:
            if self.blocking:
                raise AssertionError("blocking send on the accept thread")
            raise BlockingIOError

        def sendall(self, _data: bytes) -> None:
            raise AssertionError("sendall can block on the accept thread")

    peer = Full()
    coding_model_proxy._refuse(peer)  # type: ignore[arg-type]
    assert "setblocking:False" in peer.calls


def test_a_reply_write_timeout_is_one_log_line(proxy, monkeypatch, caplog, capsys) -> None:
    from aether_worker import coding_model_proxy

    def stalled(_writer, _status, _payload) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(coding_model_proxy, "_write_json", stalled)
    caplog.set_level("INFO", logger="aether.worker.coding_proxy")
    sock = _connect(proxy)
    try:
        raw = b'{"model":"coder","messages":[]}'
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            + f"authorization: Bearer {_JOB}\r\ncontent-length: {len(raw)}\r\n\r\n".encode()
            + raw
        )
        assert _closed_within(sock, 3.0) is not None
    finally:
        sock.close()
    assert proxy.wait_until_active(0, timeout=2.0)
    lines = [record.getMessage() for record in caplog.records if "write_timeout" in record.getMessage()]
    assert lines == ["coding_proxy status=200 error=write_timeout"]
    assert "Traceback" not in capsys.readouterr().err


def test_limits_have_sane_defaults(monkeypatch) -> None:
    from aether_worker import coding_model_proxy

    monkeypatch.delenv("AETHER_CODING_PROXY_READ_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AETHER_CODING_PROXY_MAX_CONNECTIONS", raising=False)
    timeout, limit = coding_model_proxy.proxy_limits()
    assert 5 <= timeout <= 120
    assert 2 <= limit <= 64
    monkeypatch.setenv("AETHER_CODING_PROXY_READ_TIMEOUT_SECONDS", "nonsense")
    monkeypatch.setenv("AETHER_CODING_PROXY_MAX_CONNECTIONS", "0")
    assert coding_model_proxy.proxy_limits() == (timeout, limit)
