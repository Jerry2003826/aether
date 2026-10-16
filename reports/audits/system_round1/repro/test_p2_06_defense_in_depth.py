"""P2-06 · 纵深防线上的缺口：SSRF 地址段、数据库终态守卫、MCP argv 过滤。

不变量 INV-UNTRUST-02：抓取只连公网地址（含 100.64.0.0/10 共享地址段与其中的云厂商元数据地址）；
不变量 INV-STOP-01（DB 层）：终态不能被改写成另一个终态；
不变量 INV-UNTRUST-04：MCP stdio 只接受固定 argv，不能借解释器的 -c/-e 执行一段 shell 文本。
抓取和 MCP 目前都没有接进产品路径（见 FINDINGS P2-09），这里证明的是组件本身的缺口。
"""

from __future__ import annotations

import ipaddress

import pytest
from sqlalchemy import text

from aether_gateway.mcp_client import McpError, spawn
from aether_storage.db import Database
from aether_worker.ingest import IngestError, assert_public_target
from audit_helpers import key, project, rows


@pytest.mark.parametrize("address", ["100.100.100.200", "100.64.0.1", "fec0::1"])
def test_fetch_rejects_non_public_addresses(address: str) -> None:
    ip = ipaddress.ip_address(address)
    url = f"http://[{address}]/" if ip.version == 6 else f"http://{address}/"
    with pytest.raises(IngestError):
        assert_public_target(url, resolve=lambda _host: [ip])


def test_database_refuses_to_rewrite_a_cancelled_task_as_succeeded(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "终态守卫"})
    task_id = created.json()["resource_id"]
    worker = Database(role_urls["aether_worker"])
    with worker.transaction(owner_id=owner, project_id=project_id) as conn:
        conn.execute(text("UPDATE tasks SET status = 'CANCELLED' WHERE id = :id"), {"id": task_id})
    error = None
    try:
        with worker.transaction(owner_id=owner, project_id=project_id) as conn:
            conn.execute(text("UPDATE tasks SET status = 'SUCCEEDED' WHERE id = :id"), {"id": task_id})
    except Exception as exc:
        error = type(exc).__name__
    final = rows(worker, owner, project_id, "SELECT status FROM tasks WHERE id = :id", id=task_id)[0]["status"]
    assert error is not None, f"aether_worker 角色把 CANCELLED 改成了 {final}，数据库没有拦"


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-ec", "cat /etc/passwd > /tmp/audit-mcp-proof"],
        ["bash", "-lc", "id"],
        ["node", "-e", "require('child_process').execSync('id')"],
    ],
)
def test_mcp_stdio_argv_rejects_shell_interpreters(argv: list[str]) -> None:
    with pytest.raises(McpError):
        spawn(argv)
