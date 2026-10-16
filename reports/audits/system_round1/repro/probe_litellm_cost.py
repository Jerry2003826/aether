"""P2-01 补充证据：LiteLLM 的 response_cost 来自本地价目表，并且按上游自报的模型名定价。

本机起一个 OpenAI 兼容服务，响应里只有 usage，没有任何费用字段。网关同样以 openai/<alias> 调用。
运行：uv run --locked --no-build python reports/audits/system_round1/repro/probe_litellm_cost.py
"""

from __future__ import annotations

import importlib.metadata
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import litellm

REPORTED = {"model": ""}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0")))
        body = json.dumps(
            {
                "id": "chatcmpl-probe",
                "object": "chat.completion",
                "created": 1,
                "model": REPORTED["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    print(f"litellm {importlib.metadata.version('litellm')}; usage 固定为 prompt=1000 completion=500；响应里没有费用字段")
    try:
        for requested in ("audit-model", "deepseek-chat"):
            for reported in ("audit-model", "deepseek-chat", "gpt-4o-mini", "gpt-4o"):
                REPORTED["model"] = reported
                response = litellm.completion(
                    model=f"openai/{requested}",
                    api_base=base,
                    api_key="probe-key-not-real",
                    messages=[{"role": "user", "content": "hi"}],
                    num_retries=0,
                )
                hidden = getattr(response, "_hidden_params", {}) or {}
                print(f"requested={requested:14s} upstream_reported={reported:14s} response_cost={hidden.get('response_cost')}")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
