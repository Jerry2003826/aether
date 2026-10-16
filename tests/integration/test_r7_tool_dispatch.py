"""Approved tools run through the worker, the reviewed connector, and the operation ledger."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_gateway.mcp_client import McpClient, fixture_argv, schema_hash
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_storage.operations import OperationLedger
from aether_worker.service import route_pending
from tests.integration.test_console_api import _headers, _session


def _approved_schema() -> str:
    with McpClient(fixture_argv("echo"), timeout=5) as client:
        tools = client.request("tools/list")["result"]["tools"]
    echo = next(tool for tool in tools if tool["name"] == "echo")
    assert echo["annotations"]["readOnlyHint"] is True
    return schema_hash(tools)


def _marker(key: str) -> Path:
    safe = "".join(ch for ch in key if ch.isalnum())[:64] or "default"
    return Path(f"/tmp/aether-mcp-flaky-{safe}")


def _dispatch(
    client,
    role_urls,
    monkeypatch,
    tmp_path: Path,
    *,
    name: str,
    tool_id: str,
    capability: str,
    argv: list[str],
    body: str,
    cancel: bool,
) -> tuple[str, str, str]:
    owner_id, project_id, csrf = _session(client, name)
    monkeypatch.setenv("AETHER_INDEX_ROOT", str(tmp_path / "indexes"))
    registered = client.post(
        "/v1/admin/connectors",
        headers=_headers(csrf, f"{name}-connector"),
        json={
            "project_id": project_id,
            "connector_key": name,
            "transport": "stdio",
            "manifest": {
                "argv": argv,
                "bindings": [{"tool_id": tool_id, "mcp_name": "echo", "approved_schema_hash": _approved_schema()}],
            },
        },
    )
    assert registered.status_code == 202, registered.text
    created = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers=_headers(csrf, f"{name}-task"),
        json={"goal": "把已批准的草稿交给连接器"},
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    if cancel:
        with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text("UPDATE tasks SET status = 'CANCELLED' WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": task_id},
            )
    intent = {
        "intent_key": f"{name}-intent",
        "tool_id": tool_id,
        "arguments": {"text": body},
        "resource_id": f"{name}-resource",
        "resource_version": "v1",
        "risk": "R2",
    }
    digest = sha256_text(json.dumps(intent, sort_keys=True, separators=(",", ":")))
    ledger = OperationLedger(worker)
    approval_id = ledger.request_approval(owner_id=owner_id, project_id=project_id, task_id=task_id, intent=intent, risk="R2")
    decided = client.post(
        f"/v1/projects/{project_id}/approvals/{approval_id}/decision",
        headers=_headers(csrf, f"{name}-decide", "1"),
        json={"decision": "approve", "expected_intent_hash": digest},
    )
    assert decided.status_code == 202, decided.text
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text(
                "INSERT INTO capability_grants (project_id, task_id, grantee_service, token_hash, capability_type, scope, expires_at, policy_version) "
                "VALUES (:project_id, :task_id, 'worker', :token_hash, :capability, '{}'::jsonb, now() + interval '10 minutes', 'v1')"
            ),
            {"project_id": project_id, "task_id": task_id, "token_hash": uuid.uuid4().hex + uuid.uuid4().hex, "capability": capability},
        )
    ledger.note_resource_version(owner_id=owner_id, project_id=project_id, resource_id=intent["resource_id"], version="v1")
    route_pending(
        Database(role_urls["aether_dispatcher"]),
        worker,
        Database(role_urls["aether_verifier"]),
        ArtifactStore(tmp_path / "artifacts", worker),
        {},
    )
    return owner_id, project_id, task_id


def test_worker_dispatches_an_approved_tool_through_mcp_and_the_ledger(client, role_urls, monkeypatch, tmp_path: Path) -> None:
    body = "草稿已发出"
    owner_id, project_id, task_id = _dispatch(
        client,
        role_urls,
        monkeypatch,
        tmp_path,
        name="r7-tool-echo",
        tool_id="external.draft",
        capability="external:draft",
        argv=fixture_argv("echo"),
        body=body,
        cancel=False,
    )
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT tool_id, idempotency_class, risk, state, result::text AS result "
                "FROM operations WHERE project_id = :project_id AND task_id = :task_id"
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().all()
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
    assert status == "QUEUED"
    assert len(rows) == 1
    assert rows[0]["tool_id"] == "external.draft"
    assert rows[0]["idempotency_class"] == "idempotent_write"
    assert rows[0]["risk"] == "R2"
    assert rows[0]["state"] == "CONFIRMED"
    assert body in str(rows[0]["result"])


def test_idempotent_unknown_reconciles_once_and_terminal_status_stays(client, role_urls, monkeypatch, tmp_path: Path) -> None:
    draft_key = uuid.uuid4().hex
    notify_key = uuid.uuid4().hex
    _marker(draft_key).unlink(missing_ok=True)
    _marker(notify_key).unlink(missing_ok=True)
    draft = "可以再读一次"
    owner_id, project_id, task_id = _dispatch(
        client,
        role_urls,
        monkeypatch,
        tmp_path,
        name="r7-tool-flaky",
        tool_id="external.draft",
        capability="external:draft",
        argv=[sys.executable, "tests/fixtures/mcp_fixture.py", "flaky", draft_key],
        body=draft,
        cancel=False,
    )
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        draft_row = conn.execute(
            text("SELECT state, result::text AS result FROM operations WHERE project_id = :project_id AND task_id = :task_id"),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().one()
    assert _marker(draft_key).read_text(encoding="utf-8") == "2"
    assert draft_row["state"] == "CONFIRMED"
    assert draft in str(draft_row["result"])

    notify = "不要再发一次"
    notify_owner, notify_project, notify_task = _dispatch(
        client,
        role_urls,
        monkeypatch,
        tmp_path,
        name="r7-tool-notify",
        tool_id="external.notify",
        capability="external:notify",
        argv=[sys.executable, "tests/fixtures/mcp_fixture.py", "flaky", notify_key],
        body=notify,
        cancel=True,
    )
    with worker.transaction(owner_id=notify_owner, project_id=notify_project) as conn:
        notify_row = conn.execute(
            text("SELECT state, idempotency_class FROM operations WHERE project_id = :project_id AND task_id = :task_id"),
            {"project_id": notify_project, "task_id": notify_task},
        ).mappings().one()
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": notify_task}).scalar_one()
        count = conn.execute(
            text("SELECT count(*) FROM operations WHERE project_id = :project_id AND task_id = :task_id"),
            {"project_id": notify_project, "task_id": notify_task},
        ).scalar_one()
    assert _marker(notify_key).read_text(encoding="utf-8") == "1"
    assert notify_row["state"] == "UNKNOWN"
    assert notify_row["idempotency_class"] == "non_idempotent"
    assert status == "CANCELLED"
    assert int(count) == 1
