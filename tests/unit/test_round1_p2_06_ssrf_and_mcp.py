"""System audit round 1, P2-06: fetch targets outside public address space and MCP argv that runs inline code.

INV-UNTRUST-02: fetches reach public addresses only, including the 100.64.0.0/10 shared range and the
cloud metadata address inside it. INV-UNTRUST-04: MCP stdio takes a fixed argv and cannot use an
interpreter's -c/-e to run a shell line.
"""

from __future__ import annotations

import ipaddress

import pytest

from aether_gateway.mcp_client import McpError, spawn
from aether_worker.ingest import IngestError, assert_public_target


@pytest.mark.parametrize("address", ["100.100.100.200", "100.64.0.1", "fec0::1"])
def test_fetch_rejects_non_public_addresses(address: str) -> None:
    ip = ipaddress.ip_address(address)
    url = f"http://[{address}]/" if ip.version == 6 else f"http://{address}/"
    with pytest.raises(IngestError):
        assert_public_target(url, resolve=lambda _host: [ip])


@pytest.mark.parametrize("address", ["::ffff:127.0.0.1", "::ffff:100.64.0.1", "64:ff9b::a9fe:a9fe"])
def test_fetch_rejects_ipv6_forms_of_blocked_ipv4(address: str) -> None:
    ip = ipaddress.ip_address(address)
    with pytest.raises(IngestError):
        assert_public_target("https://example.test/", resolve=lambda _host: [ip])


def test_fetch_still_allows_a_public_address() -> None:
    ip = ipaddress.ip_address("1.1.1.1")
    assert assert_public_target("https://example.test/", resolve=lambda _host: [ip]) == "1.1.1.1"


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


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/dash", "server.sh"],
        ["env", "sh", "server.sh"],
        ["node", "--eval=process.exit(0)"],
        ["node22", "-p", "1"],
        ["python3.13", "-Ic", "print(1)"],
        ["perl", "-we", "print 1"],
        ["ruby", "-e", "puts 1"],
        ["php", "-r", "echo 1;"],
    ],
)
def test_mcp_stdio_argv_rejects_other_inline_code_forms(argv: list[str]) -> None:
    with pytest.raises(McpError) as rejected:
        spawn(argv)
    assert rejected.value.code == "shell_rejected"


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "-m", "mcp_server_fetch"],
        ["node", "server.js", "--port", "0"],
        ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"],
    ],
)
def test_mcp_stdio_argv_keeps_fixed_program_launches(argv: list[str]) -> None:
    assert spawn(argv) == argv
