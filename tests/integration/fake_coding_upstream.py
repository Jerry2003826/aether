"""Test-only coding upstream. It answers the gateway like a provider and really edits app.py.

Production code has no scripted coding model: without an upstream the gateway answers
model_unavailable. Tests that need a passing coding job start this server on 127.0.0.1
and point the gateway's upstream (AETHER_LIVE_MODEL_BASE) at it. The sandbox stays
--network none; its calls reach this server through the job's model socket proxy and
the gateway on the host.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROVIDER_KEY = "gateway-provider-key"


class FakeCodingModel:
    """Replies with one tool call per request: by default a file_editor edit of app.py, then finish.

    The round is chosen from the conversation itself (how many tool results it already holds),
    so several jobs served by one instance each start from the first round.
    """

    def __init__(
        self, rounds: tuple[tuple[str, int, int], ...] | None = None, *, provider_key: str = PROVIDER_KEY
    ) -> None:
        self.provider_key = provider_key
        self.authorizations: list[str] = []
        self.models: list[str] = []
        self.tool_names: set[str] = set()
        self.usages: list[tuple[int, int]] = []
        self.problems: list[str] = []
        self._rounds = rounds or (("edit", 20, 11), ("finish", 20, 7))
        self._calls = 0
        self._lock = threading.Lock()

    def reply(self, raw: bytes, authorization: str) -> dict:
        with self._lock:
            self.authorizations.append(authorization)
            self._calls += 1
            call = self._calls
            loaded = json.loads(raw)
            model = loaded.get("model")
            if isinstance(model, str):
                self.models.append(model)
            for tool in loaded.get("tools") or []:
                function = tool.get("function") if isinstance(tool, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    self.tool_names.add(name)
            text_body = raw.decode("utf-8", "replace")
            if "aether_job_token" in loaded:
                self.problems.append("job token reached the model")
            if self.provider_key in text_body:
                self.problems.append("provider key reached the model body")
        del call
        answered = sum(1 for item in loaded.get("messages") or [] if isinstance(item, dict) and item.get("role") == "tool")
        kind, prompt, completion = self._rounds[min(answered, len(self._rounds) - 1)]
        target = _app_path(text_body)
        if kind == "edit" and not target.startswith("/"):
            self.problems.append("missing app.py path")
        if kind == "view":
            arguments = json.dumps({"command": "view", "path": target})
            name = "file_editor"
            call_id = "call_view"
        elif kind == "edit":
            arguments = json.dumps({"command": "str_replace", "path": target, "old_str": "return 0", "new_str": "return 42"})
            name = "file_editor"
            call_id = "call_edit"
        else:
            arguments = json.dumps({"message": "done"})
            name = "finish"
            call_id = "call_finish"
        with self._lock:
            self.usages.append((prompt, completion))
        return {
            "id": call_id,
            "object": "chat.completion",
            "created": 0,
            "model": "coder",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
        }


_REPOSITORY = re.compile(r"The repository is the directory (/[^;\s\"]+);")


def _app_path(text: str) -> str:
    """app.py under the workspace the entry names in its system prompt.

    The user message is the job objective verbatim and carries no sandbox path.
    """
    found = _REPOSITORY.search(text)
    if found is None:
        return ""
    return f"{found.group(1)}/app.py"


def _handler(model: FakeCodingModel) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            raw = self.rfile.read(length)
            payload = model.reply(raw, self.headers.get("authorization") or "")
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler




@contextmanager
def editing_upstream(model: FakeCodingModel) -> Iterator[str]:
    """Serve ``model`` on 127.0.0.1 for the duration of the block; yields the ``/v1`` base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(model))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
