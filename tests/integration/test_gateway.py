import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest

from aether_gateway.model import GatewayDenied, ModelGateway
from aether_gateway.registry import ToolDenied, admit_tool, load_registry
from aether_storage.db import Database
from tests.integration.test_auth import _bootstrap

PROVIDER_KEY = "sk-test-secret-not-for-logs"


class Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length)
        Upstream.seen.append({"path": self.path, "authorization": self.headers.get("authorization"), "body": json.loads(raw.decode())})
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "upstream-ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        return


@pytest.fixture
def upstream():
    Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()


def test_gateway_uses_token_scope_and_real_upstream(client, api_db, role_urls, aether_admin, upstream) -> None:
    script = Path("migrations/sql/0004_gateway_lookup.sql").read_text(encoding="utf-8")
    with psycopg.connect(aether_admin, autocommit=True) as conn:
        conn.execute(script)
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    project = client.post(
        "/v1/projects",
        headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "gateway-project"},
        json={"name": "网关"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    issuer = ModelGateway(api_db, upstream_base=upstream, provider_key=PROVIDER_KEY)
    token = issuer.issue_token(owner_id=session["owner_id"], project_id=project_id, capabilities=["knowledge:read"], aliases=["actor"], egress=["https://example.test"])
    gateway = ModelGateway(Database(role_urls["aether_worker"]), upstream_base=upstream, provider_key=PROVIDER_KEY)
    result = gateway.complete(token, {"alias": "actor", "project_id": "other-project", "task_id": "other-task", "prompt": "hello"})
    assert result["project_id"] == project_id
    assert result["output"] == "upstream-ok"
    assert result["usage_status"] == "known"
    assert len(Upstream.seen) == 1
    assert Upstream.seen[0]["authorization"] == f"Bearer {PROVIDER_KEY}"
    assert PROVIDER_KEY not in json.dumps(result)
    assert all(PROVIDER_KEY not in line for line in gateway.audit)
    registry = load_registry(Path("config/tools.example.yaml"))
    scope = {"project_id": project_id, "capabilities": ["knowledge:read"], "egress": ["https://example.test"]}
    admitted = admit_tool(registry, scope, "knowledge.search", {"query": "证据"})
    assert admitted["project_id"] == project_id
    with pytest.raises(ToolDenied):
        admit_tool(registry, scope, "provider.read_key", {})
    with pytest.raises(ToolDenied):
        admit_tool(registry, scope, "knowledge.search", {"destination": "https://evil.test"})
    with pytest.raises(GatewayDenied):
        gateway.complete("not-a-token", {"alias": "actor", "prompt": "no"})
