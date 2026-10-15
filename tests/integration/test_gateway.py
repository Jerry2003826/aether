import json
import threading
import time
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
    mode = "ok"
    body_text = "upstream-ok"
    delay = 0.0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length)
        Upstream.seen.append({"path": self.path, "authorization": self.headers.get("authorization"), "body": json.loads(raw.decode())})
        if Upstream.delay:
            time.sleep(Upstream.delay)
        if Upstream.mode == "error":
            payload = b'{"error":"upstream"}'
            self.send_response(500)
        else:
            content = self.headers.get("authorization") if Upstream.mode == "echo" else Upstream.body_text
            payload = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }
            ).encode()
            self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


@pytest.fixture
def upstream():
    Upstream.seen = []
    Upstream.mode = "ok"
    Upstream.body_text = "upstream-ok"
    Upstream.delay = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()


def _gateway(client, api_db, role_urls, aether_admin, upstream, *, timeout=5):
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
    issuer = ModelGateway(api_db, upstream_base=upstream, provider_key=PROVIDER_KEY, timeout=timeout)
    token = issuer.issue_token(
        owner_id=session["owner_id"],
        project_id=project_id,
        capabilities=["knowledge:read"],
        aliases=["actor"],
        egress=["https://example.test"],
    )
    gateway = ModelGateway(Database(role_urls["aether_worker"]), upstream_base=upstream, provider_key=PROVIDER_KEY, timeout=timeout)
    return project_id, token, gateway


def test_unauthorized_tool_and_egress_are_denied() -> None:
    registry = load_registry(Path("config/tools.example.yaml"))
    scope = {"project_id": "project-1", "capabilities": ["knowledge:read"], "egress": ["https://example.test"]}
    with pytest.raises(ToolDenied) as denied:
        admit_tool(registry, scope, "provider.read_key", {})
    assert denied.value.code == "tool_denied"
    with pytest.raises(ToolDenied) as missing:
        admit_tool(registry, scope, "not.a.tool", {})
    assert missing.value.code == "tool_denied"
    with pytest.raises(ToolDenied) as egress:
        admit_tool(registry, scope, "knowledge.search", {"destination": "https://evil.test"})
    assert egress.value.code == "egress_denied"


def test_readonly_tool_still_checks_egress() -> None:
    registry = load_registry(Path("config/tools.example.yaml"))
    scope = {"project_id": "project-1", "capabilities": ["knowledge:read"], "egress": []}
    with pytest.raises(ToolDenied) as egress:
        admit_tool(registry, scope, "knowledge.search", {"query": "证据", "destination": "https://example.test"})
    assert egress.value.code == "egress_denied"


def test_body_cannot_override_token_project_or_task(client, api_db, role_urls, aether_admin, upstream) -> None:
    project_id, token, gateway = _gateway(client, api_db, role_urls, aether_admin, upstream)
    result = gateway.complete(token, {"alias": "actor", "project_id": "other-project", "task_id": "other-task", "prompt": "hello"})
    assert result["project_id"] == project_id
    assert result["task_id"] != "other-task"
    assert Upstream.seen[0]["path"].endswith("/chat/completions")


def test_provider_key_is_redacted_from_result_and_audit(client, api_db, role_urls, aether_admin, upstream) -> None:
    Upstream.mode = "echo"
    _project, token, gateway = _gateway(client, api_db, role_urls, aether_admin, upstream)
    result = gateway.complete(token, {"alias": "actor", "prompt": "hello"})
    assert PROVIDER_KEY not in result["output"]
    assert "[REDACTED]" in result["output"]
    assert all(PROVIDER_KEY not in line for line in gateway.audit)


def test_litellm_uses_real_upstream_response(client, api_db, role_urls, aether_admin, upstream) -> None:
    Upstream.body_text = "specific-upstream-text"
    _project, token, gateway = _gateway(client, api_db, role_urls, aether_admin, upstream)
    result = gateway.complete(token, {"alias": "actor", "prompt": "hello"})
    assert result["output"] == "specific-upstream-text"
    assert result["usage_status"] == "known"
    assert Upstream.seen[0]["authorization"] == f"Bearer {PROVIDER_KEY}"
    assert len(Upstream.seen) == 1


def test_upstream_error_is_not_success(client, api_db, role_urls, aether_admin, upstream) -> None:
    Upstream.mode = "error"
    _project, token, gateway = _gateway(client, api_db, role_urls, aether_admin, upstream)
    with pytest.raises(GatewayDenied) as denied:
        gateway.complete(token, {"alias": "actor", "prompt": "hello"})
    assert denied.value.code == "upstream_error"
    assert len(Upstream.seen) == 1


def test_upstream_timeout_is_unknown_not_zero(client, api_db, role_urls, aether_admin, upstream) -> None:
    Upstream.delay = 1.5
    _project, token, gateway = _gateway(client, api_db, role_urls, aether_admin, upstream, timeout=0.2)
    result = gateway.complete(token, {"alias": "actor", "prompt": "hello"})
    assert result["usage_status"] == "unknown"
    assert result["actual_cost_micro_usd"] is None
    assert result["output"] is None
    assert len(Upstream.seen) == 1
