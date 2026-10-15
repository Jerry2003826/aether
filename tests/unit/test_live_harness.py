import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aether_worker.live_harness import LIVE_ALIAS, LIVE_BASE, LIVE_KEY, MAX_OUTPUT_TOKENS, run_live_probe


def test_missing_live_key_is_blocked_and_not_a_pass(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    report = run_live_probe()
    assert report["status"] == "blocked"
    assert report["live_ready"] is False
    assert report["scripted_substitute"] is False
    assert report["research"] == "not_run"
    assert report["coding"] == "not_run"
    assert report["missing"] == [LIVE_KEY, LIVE_BASE, LIVE_ALIAS]
    assert "passed" not in report["status"]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.calls.append((self.headers.get("Authorization"), body))
        self.server.respond(self, body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self, respond) -> None:
        self.respond = respond
        self.calls: list[tuple[str | None, bytes]] = []
        super().__init__(("127.0.0.1", 0), _Handler)


def _serve(respond):
    server = _Server(respond)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server: _Server, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _env(monkeypatch, server: _Server, key: str = "sk-live-SUPERSECRET") -> None:
    host, port = server.server_address
    monkeypatch.setenv(LIVE_KEY, key)
    monkeypatch.setenv(LIVE_BASE, f"http://{host}:{port}/v1")
    monkeypatch.setenv(LIVE_ALIAS, "deepseek-chat")


def _completion(name: str, arguments: dict[str, object], *, prompt: int = 11, completion: int = 7) -> bytes:
    body = {
        "choices": [{"message": {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }
    return json.dumps(body).encode()


def _ok(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    request = json.loads(body)
    assert request["max_tokens"] == MAX_OUTPUT_TOKENS
    tool = request["tools"][0]["function"]["name"]
    if tool == "cite_finding":
        payload = _completion("cite_finding", {"claim": "标准大气压下水的沸点是100摄氏度", "citation_id": "SRC-1"})
    else:
        payload = _completion("submit_function", {"source": "def add(a, b):\n    return a + b\n"}, prompt=9, completion=6)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def test_fake_server_success_records_checks_without_the_key(monkeypatch) -> None:
    server, thread = _serve(_ok)
    try:
        _env(monkeypatch, server)
        report = run_live_probe(timeout=2)
    finally:
        _stop(server, thread)
    encoded = json.dumps(report)
    assert "sk-live-SUPERSECRET" not in encoded
    assert report["status"] == "probed"
    assert report["live_ready"] is False
    assert report["calls"] == 2
    assert report["research"] == "pass"
    assert report["coding"] == "pass"
    assert report["hybrid_retrieval"] == "not_run"
    assert report["linux_isolation"] == "not_run"
    assert report["usage"]["prompt_tokens"] == 20
    assert report["usage"]["completion_tokens"] == 13
    assert report["usage"]["cost_status"] == "unknown"
    by_id = {row["id"]: row for row in report["checks"]}
    assert by_id["protocol.tools"]["status"] == "pass"
    assert by_id["protocol.schema"]["status"] == "pass"
    assert by_id["protocol.usage"]["status"] == "pass"
    assert by_id["research.citation"]["status"] == "pass"
    assert by_id["coding.independent_test"]["status"] == "pass"
    assert by_id["coding.recovery"]["status"] == "not_run"
    assert by_id["protocol.tools"]["latency_ms"] is not None
    assert len(server.calls) == 2


def test_fake_server_auth_failure_is_not_a_pass(monkeypatch) -> None:
    def respond(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
        payload = b"unauthorized"
        handler.send_response(401)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    server, thread = _serve(respond)
    try:
        _env(monkeypatch, server)
        report = run_live_probe(timeout=2)
    finally:
        _stop(server, thread)
    assert report["status"] == "failed"
    assert report["live_ready"] is False
    assert report["research"] == "not_run"
    assert report["calls"] == 1
    assert "http_401" in report["error"]
    assert "sk-live-SUPERSECRET" not in json.dumps(report)


def test_fake_server_timeout_fails_closed(monkeypatch) -> None:
    def respond(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
        time.sleep(1.5)
        handler.send_response(200)
        handler.end_headers()

    server, thread = _serve(respond)
    try:
        _env(monkeypatch, server)
        report = run_live_probe(timeout=0.2)
    finally:
        _stop(server, thread)
    assert report["status"] == "failed"
    assert report["live_ready"] is False
    assert "imeout" in report["error"] or "timed out" in report["error"]
    assert "sk-live-SUPERSECRET" not in json.dumps(report)


def test_error_text_redacts_the_key(monkeypatch) -> None:
    key = "sk-live-SUPERSECRET"

    def respond(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
        payload = f"bad key {key}".encode()
        handler.send_response(401)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    server, thread = _serve(respond)
    try:
        _env(monkeypatch, server, key)
        report = run_live_probe(timeout=2)
    finally:
        _stop(server, thread)
    encoded = json.dumps(report)
    assert key not in encoded
    assert "[REDACTED]" in encoded
    assert report["status"] == "failed"


def test_script_writes_blocked_result_without_calling_out(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    path = Path("scripts/run_f26_live.py")
    spec = importlib.util.spec_from_file_location("run_f26_live", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = Path("reports/f26_live_result.json")
    try:
        code = module.main(["--out", "reports/f26_live_result.json"])
        written = json.loads(destination.read_text(encoding="utf-8"))
    finally:
        destination.unlink(missing_ok=True)
    assert code == 2
    assert written["status"] == "blocked"
    assert written["live_ready"] is False
