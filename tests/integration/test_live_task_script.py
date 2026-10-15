"""The live-task script must get past upload and call the configured upstream."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.integration.test_worker_process import _temporal_address

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "reports" / "live_task_check.json"
TOKEN = "local-upstream-token"
FACT = "事实：水在标准大气压下于一百摄氏度沸腾"


class _Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length)
        body = json.loads(raw.decode())
        _Upstream.seen.append({"path": self.path, "body": body, "authorization": self.headers.get("authorization")})
        payload = json.dumps(
            {
                "id": "chatcmpl-script",
                "object": "chat.completion",
                "model": "fake-upstream-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"资料记载：{FACT}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def test_live_task_check_script_calls_the_fake_upstream(role_urls, clean_db) -> None:
    address = _temporal_address()
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous = RESULT.read_bytes() if RESULT.exists() else None
    env = os.environ.copy()
    env.update(
        {
            "AETHER_DATABASE_URL": role_urls["aether_api"],
            "AETHER_WORKER_DATABASE_URL": role_urls["aether_worker"],
            "AETHER_VERIFIER_DATABASE_URL": role_urls["aether_verifier"],
            "AETHER_DISPATCHER_DATABASE_URL": role_urls["aether_dispatcher"],
            "AETHER_TEMPORAL_ADDRESS": address,
            "AETHER_LIVE_MODEL_BASE": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "AETHER_LIVE_MODEL_ALIAS": "research-alias",
            "AETHER_LIVE_MODEL_KEY": TOKEN,
            "AETHER_RESEARCH_PROVIDER": "live",
            # The checkout is home, so data/ inside the repo is not a legal artifact root.
            "HOME": str(ROOT),
        }
    )
    try:
        completed = subprocess.run(
            [sys.executable, "scripts/run_live_task_check.py", "--out", "reports/live_task_check.json"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        written = RESULT.read_text(encoding="utf-8") if RESULT.exists() else ""
        assert completed.returncode == 0, completed.stdout + completed.stderr + written
        report = json.loads(written)
        assert report["status"] == "probed"
        assert report["research_calls"] == 1
        assert report["task_status"] == "SUCCEEDED"
        assert TOKEN not in completed.stdout
        assert TOKEN not in completed.stderr
        assert TOKEN not in written
        assert _Upstream.seen
        prompt = _Upstream.seen[0]["body"]["messages"][0]["content"]
        assert FACT in prompt
        assert _Upstream.seen[0]["authorization"] == f"Bearer {TOKEN}"
    finally:
        server.shutdown()
        if previous is None:
            RESULT.unlink(missing_ok=True)
        else:
            RESULT.write_bytes(previous)
