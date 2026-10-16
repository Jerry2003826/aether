"""Public-target checks shared by ingestion and the threat browser."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

FIXTURE_ENV = "AETHER_INGEST_ALLOW_FIXTURES"
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class IngestError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped
        if embedded is None and address in _NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if address.is_site_local or (embedded is not None and _blocked(embedded)):
            return True
    return bool(
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise IngestError("dns_rejected") from exc
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except ValueError as exc:
            raise IngestError("dns_rejected") from exc
    if not found:
        raise IngestError("dns_rejected")
    return found


def fixtures_enabled() -> bool:
    return os.environ.get(FIXTURE_ENV) == "1"


def assert_public_target(
    url: str,
    *,
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]] = resolve_host,
    allow_hosts: set[str] | None = None,
) -> str:
    """Return the address to connect to. The URL hostname stays the TLS identity."""
    if allow_hosts and not fixtures_enabled():
        raise IngestError("fixture_hosts_rejected")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not host:
        raise IngestError("ssrf_rejected")
    permitted = fixtures_enabled() and bool(allow_hosts) and host in allow_hosts
    if parsed.port not in {None, 80, 443} and not permitted:
        raise IngestError("ssrf_rejected")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [literal] if literal is not None else resolve(host)
    if any(address is None or _blocked(address) for address in addresses) and not permitted:
        raise IngestError("ssrf_rejected")
    chosen = next((address for address in addresses if isinstance(address, ipaddress.IPv4Address)), None)
    if chosen is None:
        chosen = addresses[0] if addresses else None
    if chosen is None:
        raise IngestError("dns_rejected")
    return str(chosen)
