"""A cross-origin upstream redirect must never carry the provider key to the new host."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest
from aether_gateway import model
from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.db import Database

_KEY = "unit-provider-key"
_BODY = {"model": "coder", "messages": [{"role": "user", "content": "edit"}]}
_ANSWER = {
    "id": "c1",
    "model": "coder-upstream",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
}
_CREDENTIAL_HEADERS = ("authorization", "x-api-key", "api-key")


class _Server:
    """A local host: /v1/chat/completions answers with `redirect` (status, location) if set, anything else answers 200."""

    def __init__(self) -> None:
        self.redirect: tuple[int, str] | None = None
        self.seen: list[dict[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def _handle(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or "0"))
                server.seen.append({name.lower(): value for name, value in self.headers.items()} | {":path": self.path})
                if server.redirect is not None and self.path == "/v1/chat/completions":
                    status, location = server.redirect
                    self.send_response(status)
                    self.send_header("location", location)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                raw = json.dumps(_ANSWER).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_POST = _handle
            do_GET = _handle

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()


@pytest.fixture
def servers() -> Any:
    opened: list[_Server] = []

    def make() -> _Server:
        item = _Server()
        opened.append(item)
        return item

    yield make
    for item in opened:
        item.close()


def _gateway(base: str) -> ModelGateway:
    return ModelGateway(cast(Database, object()), upstream_base=base, provider_key=_KEY, timeout=5)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_cross_origin_redirect_is_refused_and_the_new_host_sees_no_credentials(servers: Any, status: int) -> None:
    upstream, elsewhere = servers(), servers()
    upstream.redirect = (status, elsewhere.origin + "/v1/chat/completions")
    denied: GatewayDenied | None = None
    try:
        _gateway(upstream.origin + "/v1")._post_upstream(dict(_BODY))
    except GatewayDenied as exc:
        denied = exc
    # Checked first, so a followed redirect shows which credential reached the other host.
    for request in elsewhere.seen:
        for header in _CREDENTIAL_HEADERS:
            assert header not in request, (header, request)
    assert elsewhere.seen == []
    # Settled like any other known non-2xx answer: the call was sent and its status is kept.
    assert denied is not None
    assert denied.code == "upstream_error"
    assert denied.sent is True
    assert denied.upstream_status == status


@pytest.mark.parametrize("status", [301, 302, 303])
def test_a_same_origin_redirect_is_still_followed(servers: Any, status: int) -> None:
    upstream = servers()
    upstream.redirect = (status, upstream.origin + "/v2/answer")
    result = _gateway(upstream.origin + "/v1")._post_upstream(dict(_BODY))
    assert result["model"] == "coder-upstream"
    assert [request[":path"] for request in upstream.seen] == ["/v1/chat/completions", "/v2/answer"]
    assert upstream.seen[1]["authorization"] == f"Bearer {_KEY}"


@pytest.mark.parametrize(
    ("current", "target", "same"),
    [
        ("https://api.example.com/v1/chat/completions", "https://api.example.com/v2/x", True),
        ("https://api.example.com/v1/chat/completions", "https://api.example.com:443/v2/x", True),
        ("http://api.example.com/v1", "http://API.example.com:80/v2", True),
        ("https://api.example.com/v1", "http://api.example.com/v1", False),
        ("https://api.example.com/v1", "https://api.example.com:8443/v1", False),
        ("https://api.example.com/v1", "https://evil.example.net/v1", False),
        ("http://127.0.0.1:8000/v1", "http://localhost:8000/v1", False),
    ],
)
def test_only_scheme_host_and_port_together_make_the_same_origin(current: str, target: str, same: bool) -> None:
    assert model._same_origin(current, target) is same
