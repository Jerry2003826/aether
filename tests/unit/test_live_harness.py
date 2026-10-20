import importlib.util
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aether_worker.live_harness import (
    GOLD_ID,
    LIVE_ALIAS,
    LIVE_BASE,
    LIVE_KEY,
    MAX_OUTPUT_TOKENS,
    _hybrid_context,
    _isolation_command,
    _linux_isolation,
    run_live_probe,
)


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


def _no_docker(_secret: str) -> dict[str, str | None]:
    return {"status": "not_run", "reason": "docker_missing"}


def _pass_docker(_secret: str) -> dict[str, str | None]:
    return {"status": "pass", "reason": None}


def _ok(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    request = json.loads(body)
    assert request["max_tokens"] == MAX_OUTPUT_TOKENS
    content = request["messages"][0]["content"]
    tool = request["tools"][0]["function"]["name"]
    if tool == "cite_finding":
        assert "chunk-apple" in content
        payload = _completion(
            "cite_finding",
            {"claim": "苹果派的烘烤温度是一百八十度", "citation_id": "chunk-apple", "start_offset": 0, "end_offset": 14},
            prompt=11,
            completion=7,
        )
    elif "return a - b" in content:
        payload = _completion(
            "submit_function",
            {"source": "def add(a, b):\n    return a - b\n"},
            prompt=20,
            completion=8,
        )
    else:
        assert "add(1, 2)=-1 expected 3" in content
        payload = _completion(
            "submit_function",
            {"source": "def add(a, b):\n    return a + b\n"},
            prompt=30,
            completion=9,
        )
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def test_hybrid_context_uses_bm25_and_measured_vectors() -> None:
    found = _hybrid_context()
    assert found["status"] == "pass"
    assert found["gold_id"] == GOLD_ID
    assert found["rrf"][0] == GOLD_ID
    assert GOLD_ID in found["bm25"]
    assert GOLD_ID in found["dense"]


def test_missing_docker_is_not_run(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name, *args, **kwargs: None)
    # No CLI and no daemon the SDK can reach either: the SDK alone is enough to run the check.
    monkeypatch.setattr(
        "aether_worker.runner.execute_docker",
        lambda args, timeout=None: subprocess.CompletedProcess(["docker", *args], 1, "", "sdk_unreachable"),
    )
    found = _linux_isolation("sk-live-SUPERSECRET")
    assert found["status"] == "not_run"
    assert found["reason"] == "docker_missing"
    assert "sk-live-SUPERSECRET" not in json.dumps(found)
    assert all("sk-live-SUPERSECRET" not in part for part in _isolation_command())


def test_fake_server_success_records_checks_without_the_key(monkeypatch) -> None:
    server, thread = _serve(_ok)
    try:
        _env(monkeypatch, server)
        report = run_live_probe(timeout=2, isolate=_pass_docker)
    finally:
        _stop(server, thread)
    encoded = json.dumps(report)
    assert "sk-live-SUPERSECRET" not in encoded
    assert report["status"] == "probed"
    assert report["live_ready"] is False
    assert report["calls"] == 3
    assert report["research"] == "pass"
    assert report["coding"] == "pass"
    assert report["hybrid_retrieval"] == "pass"
    assert report["linux_isolation"] == "pass"
    assert report["usage"]["prompt_tokens"] == 61
    assert report["usage"]["completion_tokens"] == 24
    assert report["usage"]["cost_status"] == "unknown"
    by_id = {row["id"]: row for row in report["checks"]}
    assert by_id["protocol.tools"]["status"] == "pass"
    assert by_id["protocol.schema"]["status"] == "pass"
    assert by_id["protocol.usage"]["status"] == "pass"
    assert by_id["research.citation"]["status"] == "pass"
    assert by_id["coding.first_attempt"]["status"] == "fail"
    assert by_id["coding.first_attempt"]["prompt_tokens"] == 20
    assert "expected 3" in by_id["coding.first_attempt"]["reason"]
    assert by_id["coding.independent_test"]["status"] == "pass"
    assert by_id["coding.independent_test"]["prompt_tokens"] == 30
    assert by_id["coding.recovery"]["status"] == "pass"
    assert by_id["coding.recovery"]["call_id"] == "call-3"
    assert by_id["coding.first_attempt"]["call_id"] == "call-2"
    assert by_id["hybrid_retrieval"]["status"] == "pass"
    assert report["hybrid"]["rrf"][0] == GOLD_ID
    assert report["hybrid"]["bm25"]
    assert report["hybrid"]["dense"]
    assert report["calls_detail"][1]["submission_sha256"] != report["calls_detail"][2]["submission_sha256"]
    assert by_id["protocol.tools"]["latency_ms"] is not None
    assert len(server.calls) == 3


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
        report = run_live_probe(timeout=2, isolate=_no_docker)
    finally:
        _stop(server, thread)
    assert report["status"] == "failed"
    assert report["live_ready"] is False
    assert report["research"] == "not_run"
    assert report["linux_isolation"] == "not_run"
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
        report = run_live_probe(timeout=0.2, isolate=_no_docker)
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
        report = run_live_probe(timeout=2, isolate=_no_docker)
    finally:
        _stop(server, thread)
    encoded = json.dumps(report)
    assert key not in encoded
    assert "[REDACTED]" in encoded
    assert report["status"] == "failed"


def test_script_writes_blocked_result_without_calling_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    path = Path("scripts/run_f26_live.py")
    spec = importlib.util.spec_from_file_location("run_f26_live", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "f26_live_result.json"
    module.RESULT_PATH = destination
    code = module.main(["--out", "reports/f26_live_result.json"])
    written = json.loads(destination.read_text(encoding="utf-8"))
    assert code == 2
    assert written["status"] == "blocked"
    assert written["live_ready"] is False
