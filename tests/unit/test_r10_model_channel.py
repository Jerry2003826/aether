"""Grading never hands candidate code the model channel: no job token or socket env, and the socket is gone."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from aether_storage.db import Database
from aether_worker.coding_model_proxy import CodingModelProxy

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_JOB = "job-token-model-channel"
_MODEL_ENV = {
    "AETHER_JOB_TOKEN": _JOB,
    "AETHER_MODEL_SOCKET": "/run/aether/proxy/model.sock",
    "AETHER_MODEL_ALIAS": "openai/coder",
    "AETHER_OBJECTIVE": "make value() return 42",
    "OPENAI_API_KEY": "sk-should-not-leak",
    "LLM_API_KEY": "llm-should-not-leak",
}
_PROBE = (
    "import os\n\n"
    "NAMES = ['AETHER_JOB_TOKEN', 'AETHER_MODEL_SOCKET', 'AETHER_MODEL_ALIAS', 'AETHER_OBJECTIVE', "
    "'OPENAI_API_KEY', 'LLM_API_KEY']\n\n"
    "def seen():\n    return sorted(name for name in NAMES if name in os.environ)\n"
)


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_neither_the_candidate_nor_the_test_process_sees_model_or_credential_env(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "app.py": _PROBE,
            "test_env.py": (
                "import os\nimport app\n\n"
                "def test_candidate_process_has_no_model_channel():\n    assert app.seen() == []\n\n"
                "def test_test_process_has_no_model_channel():\n"
                "    assert [name for name in app.NAMES if name in os.environ] == []\n"
            ),
        },
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("AETHER_")
    }
    env.update(_MODEL_ENV)
    env["AETHER_PROTOCOL_SHM_DIR"] = "/dev/shm"  # required by the protocol; the entry names the emptied one
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    body = json.loads(
        next(
            line
            for line in reversed(completed.stdout.splitlines())
            if line.startswith("{")
        )
    )
    assert {
        case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]
    } == {
        "test_env.test_candidate_process_has_no_model_channel": ("PASS", ""),
        "test_env.test_test_process_has_no_model_channel": ("PASS", ""),
    }, body


class _Gateway(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or "0"))
        encoded = json.dumps(
            {"id": "ok", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        ).encode()
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
    monkeypatch.setenv(
        "AETHER_GATEWAY_BASE", f"http://127.0.0.1:{server.server_address[1]}"
    )
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-model-channel")
    monkeypatch.delenv("AETHER_LIVE_MODEL_KEY", raising=False)
    made = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    try:
        yield made
    finally:
        made.close()
        server.shutdown()
        server.server_close()


def _send(path: Path, target: str, token: str, body: bytes = b"") -> int:
    request = (
        f"POST {target} HTTP/1.1\r\nauthorization: Bearer {token}\r\ncontent-length: {len(body)}\r\nconnection: close\r\n\r\n"
    ).encode("ascii") + body
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall(request)
        chunks = []
        while block := sock.recv(65536):
            chunks.append(block)
    return int(b"".join(chunks).split(b" ", 2)[1])


def _chat(path: Path) -> int:
    return _send(
        path,
        "/v1/chat/completions",
        _JOB,
        json.dumps({"model": "coder", "messages": []}).encode(),
    )


def test_revoking_the_job_token_closes_and_removes_the_model_socket(
    proxy: CodingModelProxy,
) -> None:
    assert _chat(proxy.socket_path) == 200
    assert _send(proxy.socket_path, "/aether/revoke", _JOB) == 200
    assert proxy.wait_until_revoked(timeout=5)
    assert not proxy.socket_path.exists()
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock,
        pytest.raises(OSError),
    ):
        sock.connect(str(proxy.socket_path))
    assert proxy.drain(timeout=1) == 0


def test_only_the_job_token_can_revoke(proxy: CodingModelProxy) -> None:
    assert _send(proxy.socket_path, "/aether/revoke", "not-the-job-token") == 401
    assert proxy.socket_path.exists()
    assert _chat(proxy.socket_path) == 200


def test_the_token_is_compared_in_constant_time_for_chat_and_revoke(
    proxy: CodingModelProxy, monkeypatch
) -> None:
    import secrets

    from aether_worker import coding_model_proxy

    calls: list[tuple[str, str]] = []

    def compare(presented, expected):
        calls.append((presented, expected))
        return secrets.compare_digest(presented, expected)

    # secrets.compare_digest is hmac.compare_digest; the proxy calls it through secrets.
    monkeypatch.setattr(
        coding_model_proxy, "secrets", types.SimpleNamespace(compare_digest=compare)
    )
    assert _send(proxy.socket_path, "/aether/revoke", "wrong") == 401
    assert _chat(proxy.socket_path) == 200
    assert calls == [
        ("Bearer wrong", f"Bearer {_JOB}"),
        (f"Bearer {_JOB}", f"Bearer {_JOB}"),
    ]


def test_concurrent_and_repeated_revokes_unlink_and_close_exactly_once(
    proxy: CodingModelProxy, monkeypatch
) -> None:
    unlinks: list[Path] = []
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == proxy.socket_path:
            unlinks.append(self)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    closes: list[int] = []
    real_close = proxy._server.server_close

    def server_close() -> None:
        closes.append(1)
        real_close()

    monkeypatch.setattr(proxy._server, "server_close", server_close)
    start = threading.Barrier(8)
    answers: list[object] = []

    def by_host() -> None:
        start.wait()
        proxy.revoke()

    def by_sandbox() -> None:
        start.wait()
        try:
            answers.append(_send(proxy.socket_path, "/aether/revoke", _JOB))
        except OSError as exc:
            # Gone (unlinked), or queued when the listener closed (reset): a closed channel too.
            answers.append(type(exc).__name__)

    threads = [threading.Thread(target=by_host) for _ in range(4)] + [
        threading.Thread(target=by_sandbox) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive(), "a revoke is stuck (deadlock?)"
    assert proxy.revoked
    assert not proxy.socket_path.exists()
    proxy.revoke()
    assert proxy.drain(timeout=1) == 0
    proxy.close()
    proxy.revoke()
    assert len(unlinks) == 1
    assert closes == [1]
    assert all(
        answer in {200, "FileNotFoundError", "ConnectionRefusedError", "ConnectionResetError", "BrokenPipeError"}
        for answer in answers
    ), answers


def test_a_call_in_flight_at_revoke_still_answers_and_drains(monkeypatch) -> None:
    release = threading.Event()
    entered = threading.Event()

    class _Slow(_Gateway):
        def do_POST(self) -> None:
            entered.set()
            release.wait(10)
            super().do_POST()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "AETHER_GATEWAY_BASE", f"http://127.0.0.1:{server.server_address[1]}"
    )
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-model-channel")
    made = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    statuses: list[int] = []
    try:
        caller = threading.Thread(
            target=lambda: statuses.append(_chat(made.socket_path))
        )
        caller.start()
        assert entered.wait(5)
        started = time.monotonic()
        made.revoke()
        assert time.monotonic() - started < 2, (
            "revoke must not wait for calls in flight"
        )
        assert made.active_connections == 1
        assert made.forwarding_calls == 1
        # Drain waits for the call that crossed the revoke.
        drained: list[int] = []
        drainer = threading.Thread(target=lambda: drained.append(made.drain(timeout=10)))
        drainer.start()
        drainer.join(0.5)
        assert drainer.is_alive(), "drain returned while a call was still being forwarded"
        release.set()
        caller.join(10)
        drainer.join(10)
        assert not caller.is_alive() and not drainer.is_alive(), "call or drain is stuck (deadlock?)"
        assert statuses == [200]
        assert drained == [0]
        assert made.forwarding_calls == 0
    finally:
        release.set()
        made.close()
        server.shutdown()
        server.server_close()


def test_a_call_not_yet_forwarded_when_revoke_returns_is_never_forwarded(
    monkeypatch,
) -> None:
    """Revoke is atomic with registering a call: no window between the revoked check and forwarding."""
    from aether_worker import coding_model_proxy

    forwarded: list[dict] = []
    monkeypatch.setattr(
        CodingModelProxy,
        "_forward",
        lambda _proxy, body: forwarded.append(body) or {"id": "x", "usage": {}},
    )
    made = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    real_loads = json.loads

    def loads(text, *args, **kwargs):
        # The revoke lands while the request is being read, after any up-front revoked check.
        made.revoke() if hasattr(made, "revoke") else made._revoke()
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(
        coding_model_proxy,
        "json",
        types.SimpleNamespace(
            loads=loads, dumps=json.dumps, JSONDecodeError=json.JSONDecodeError
        ),
    )
    try:
        status = _chat(made.socket_path)
    finally:
        made.close()
    assert forwarded == []
    assert status == 401


def test_after_revoke_chat_is_refused_and_never_reaches_the_upstream(monkeypatch) -> None:
    hits: list[int] = []

    class _Counting(_Gateway):
        def do_POST(self) -> None:
            hits.append(1)
            super().do_POST()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Counting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AETHER_GATEWAY_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-model-channel")
    made = CodingModelProxy(cast(Database, object()), job_token=_JOB)
    try:
        # A connection accepted before the revoke, whose request arrives after it.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as early:
            early.settimeout(5)
            early.connect(str(made.socket_path))
            # Accepted (a handler holds its slot), not just queued in the backlog that closing drops.
            assert made.wait_until_active(1, timeout=5)
            made.revoke()
            raw = json.dumps({"model": "coder", "messages": []}).encode()
            head = f"POST /v1/chat/completions HTTP/1.1\r\nauthorization: Bearer {_JOB}\r\ncontent-length: {len(raw)}\r\n\r\n"
            early.sendall(head.encode() + raw)
            answer = b""
            while block := early.recv(65536):
                answer += block
        assert answer.split(b" ", 2)[1] == b"401"
        assert b"revoked" in answer
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as late, pytest.raises(OSError):
            late.connect(str(made.socket_path))
        assert hits == []
    finally:
        made.close()
        server.shutdown()
        server.server_close()


def test_repeated_revokes_shut_the_listener_down_once(proxy: CodingModelProxy, monkeypatch) -> None:
    shutdowns: list[int] = []
    real_shutdown = proxy._server.shutdown

    def shutdown() -> None:
        shutdowns.append(1)
        real_shutdown()

    monkeypatch.setattr(proxy._server, "shutdown", shutdown)
    threads = [threading.Thread(target=proxy.revoke) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive(), "a revoke is stuck (deadlock?)"
    proxy.drain(timeout=1)
    proxy.close()
    proxy.revoke()
    assert shutdowns == [1]


def test_forwarding_calls_returns_to_zero_when_forward_raises(proxy: CodingModelProxy, monkeypatch) -> None:
    def broken(_proxy, _body):
        raise OSError("upstream down")

    monkeypatch.setattr(CodingModelProxy, "_forward", broken)
    assert _chat(proxy.socket_path) == 502
    assert proxy.forwarding_calls == 0
    assert proxy.drain(timeout=1) == 0


def test_a_non_ascii_authorization_header_is_unauthorized(proxy: CodingModelProxy) -> None:
    raw = json.dumps({"model": "coder", "messages": []}).encode()
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\nauthorization: Bearer " + _JOB.encode() + "\u00e9".encode("utf-8")
        + f"\r\ncontent-length: {len(raw)}\r\nconnection: close\r\n\r\n".encode()
        + raw
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(proxy.socket_path))
        sock.sendall(request)
        answer = b""
        while block := sock.recv(65536):
            answer += block
    assert answer.split(b" ", 2)[1] == b"401"
    assert proxy.forwarding_calls == 0
