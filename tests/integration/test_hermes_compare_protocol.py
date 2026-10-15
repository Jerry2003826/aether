"""Real pinned Hermes against a local upstream. No live provider key and no winner."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aether_worker.hermes_compare import EVENT_STREAM, HERMES_BIN, LESSON, LIVE_ALIAS, LIVE_BASE, LIVE_KEY, hermes_identity, run_four_way
from aether_worker.live_harness import _endpoint

ANSWER = '{"answer": 3}'


class _Upstream(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server: _Upstream = self.server  # type: ignore[assignment]
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        server.bodies.append(raw)
        payload = json.loads(raw)
        if payload.get("stream") is True:
            chunk = {"id": "up-1", "model": "deepseek-chat-v3", "choices": [{"index": 0, "delta": {"content": ANSWER}}]}
            done = {
                "id": "up-1",
                "model": "deepseek-chat-v3",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            }
            body = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode()
            content_type = EVENT_STREAM
        else:
            body = json.dumps(
                {
                    "id": "up-2",
                    "model": "deepseek-chat-v3",
                    "choices": [{"message": {"content": ANSWER}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                }
            ).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *_args: object) -> None:
        return


def test_pinned_hermes_identity_and_local_protocol(monkeypatch) -> None:
    assert HERMES_BIN.is_file()
    identity = hermes_identity(HERMES_BIN)
    assert identity["matches"] is True, identity
    upstream = _Upstream()
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(LIVE_KEY, "sk-live-SECRET")
    monkeypatch.setenv(LIVE_BASE, f"http://127.0.0.1:{upstream.server_address[1]}/v1")
    monkeypatch.setenv(LIVE_ALIAS, "deepseek-chat")
    monkeypatch.setenv("AETHER_DATABASE_URL", "postgres://leak")
    monkeypatch.setenv("HOME", "/home/ubuntu")
    try:
        report = run_four_way()
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
    encoded = json.dumps(report)
    assert "sk-live-SECRET" not in encoded
    assert "postgres://leak" not in encoded
    assert report["comparative_evidence"] == "not_run"
    assert report["superior"] is None
    assert report["hermes_identity"]["matches"] is True
    assert report["status"] == "probed", report["groups"]
    bodies = [json.loads(raw) for raw in upstream.bodies]
    rendered = [json.dumps(body, ensure_ascii=False) for body in bodies]
    streamed = [body for body in bodies if body.get("stream") is True]
    titled = [text for text in rendered if "name chat sessions" in text]
    memory = [text for text in rendered if "MEMORY (your personal notes)" in text]
    assert report["calls"] == len(bodies)
    assert report["calls"] <= 8
    assert len(streamed) == 2
    assert len(titled) == 2
    assert len(memory) == 1
    assert LESSON in memory[0]
    assert all("tools" not in body for body in bodies)
    assert all(body["stream"] is True for body in streamed)
    for row in report["calls_detail"]:
        assert "stream" not in row.get("dropped_fields", [])
        assert row.get("model") == "deepseek-chat-v3"
        assert row.get("requested_model") == "deepseek-chat"
    assert _endpoint(f"http://127.0.0.1:{upstream.server_address[1]}/v1").endswith("/chat/completions")
