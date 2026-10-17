"""Grading never hands candidate code the model channel: no job token or socket env, and the socket is gone."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
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
