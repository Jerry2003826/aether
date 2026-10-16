"""Authorized ingestion. Empty text is a failure. Embeddings stay unset until measured."""

from __future__ import annotations

import ssl
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpcore
import httpx
from httpx._config import create_ssl_context
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database

PARSER_VERSION = "text-1"
CHUNKER_VERSION = "codepoint-1"
MAX_HTTP_BYTES = 10 * 1024 * 1024
FIXTURE_ENV = "AETHER_INGEST_ALLOW_FIXTURES"
_REDIRECTS = {301, 302, 303, 307, 308}


class IngestError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_text(raw: str) -> str:
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def chunk_text(raw: str, *, width: int = 32) -> list[dict[str, int | str]]:
    text_value = canonical_text(raw)
    if text_value.strip() == "":
        raise IngestError("empty_body")
    if width < 1:
        raise IngestError("invalid_chunk_width")
    points = list(text_value)
    chunks: list[dict[str, int | str]] = []
    start = 0
    while start < len(points):
        end = min(start + width, len(points))
        chunks.append({"ordinal": len(chunks), "text": "".join(points[start:end]), "start_offset": start, "end_offset": end})
        start = end
    return chunks


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


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
    if not addresses or addresses[0] is None:
        raise IngestError("dns_rejected")
    return str(addresses[0])


class _PinnedBackend(httpcore.SyncBackend):
    def __init__(self, address: str) -> None:
        self._address = address

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.NetworkStream:
        return super().connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,  # type: ignore[arg-type]
        )


class _PinnedTransport(httpx.HTTPTransport):
    def __init__(self, pinned_ip: str, *, verify: ssl.SSLContext | str | bool) -> None:
        super().__init__(verify=verify, retries=0, trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=create_ssl_context(verify=verify, trust_env=False),
            network_backend=_PinnedBackend(pinned_ip),
            retries=0,
        )


def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(chunk_size=1024):
        total += len(chunk)
        if total > max_bytes:
            response.close()
            raise IngestError("size_rejected")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_url(
    url: str,
    *,
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]] = resolve_host,
    allow_hosts: set[str] | None = None,
    opener: Callable[..., httpx.Response] | None = None,
    redirects_left: int = 2,
    max_bytes: int = MAX_HTTP_BYTES,
    verify: ssl.SSLContext | str | bool = True,
) -> bytes:
    if redirects_left < 0:
        raise IngestError("redirect_rejected")
    pinned = assert_public_target(url, resolve=resolve, allow_hosts=allow_hosts)
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if opener is not None:
        response = opener(url, headers={"Host": host})
        if response.status_code in _REDIRECTS:
            return _follow(response, url, resolve=resolve, allow_hosts=allow_hosts, opener=opener, redirects_left=redirects_left, max_bytes=max_bytes, verify=verify)
        if len(response.content) > max_bytes:
            raise IngestError("size_rejected")
        return response.content
    transport = _PinnedTransport(pinned, verify=verify)
    with httpx.Client(transport=transport, verify=verify, follow_redirects=False, trust_env=False, timeout=5.0) as client:
        with client.stream("GET", url, headers={"Host": host}) as response:
            if response.status_code in _REDIRECTS:
                location = _location(response, url)
                return fetch_url(
                    location,
                    resolve=resolve,
                    allow_hosts=allow_hosts,
                    redirects_left=redirects_left - 1,
                    max_bytes=max_bytes,
                    verify=verify,
                )
            return _read_limited(response, max_bytes)


def _location(response: httpx.Response, url: str) -> str:
    location = response.headers.get("location")
    if not location:
        raise IngestError("redirect_rejected")
    if location.startswith("/"):
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}{location}"
    return location


def _follow(
    response: httpx.Response,
    url: str,
    *,
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]],
    allow_hosts: set[str] | None,
    opener: Callable[..., httpx.Response] | None,
    redirects_left: int,
    max_bytes: int,
    verify: ssl.SSLContext | str | bool,
) -> bytes:
    return fetch_url(
        _location(response, url),
        resolve=resolve,
        allow_hosts=allow_hosts,
        opener=opener,
        redirects_left=redirects_left - 1,
        max_bytes=max_bytes,
        verify=verify,
    )


def parse_rss_items(xml_bytes: bytes) -> list[str]:
    return _parse_rss_subprocess(xml_bytes, spin=False)


def _parse_rss_subprocess(xml_bytes: bytes, *, spin: bool) -> list[str]:
    env = os.environ.copy()
    worker_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = worker_root + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("AETHER_PARSER_SPIN", None)
    if spin:
        env["AETHER_PARSER_SPIN"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "aether_worker.rss_isolate"],
            input=xml_bytes,
            capture_output=True,
            timeout=3,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestError("parse_rejected") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise IngestError("parse_rejected")
    try:
        payload = json.loads(completed.stdout.decode())
    except json.JSONDecodeError as exc:
        raise IngestError("parse_rejected") from exc
    if not payload.get("ok"):
        raise IngestError("parse_rejected")
    texts = payload.get("texts")
    if not isinstance(texts, list) or not texts:
        raise IngestError("empty_body")
    if payload.get("pid") == os.getpid():
        raise IngestError("parse_rejected")
    return [str(item) for item in texts]


def store_text_version(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    kind: str,
    locator: str,
    raw: bytes,
    text_value: str,
    width: int = 32,
) -> dict[str, str]:
    chunks = chunk_text(text_value, width=width)
    canonical = canonical_text(text_value).encode()
    raw_ref = store.put(owner_id=owner_id, project_id=project_id, data=raw, media_type="text/plain")
    text_ref = store.put(owner_id=owner_id, project_id=project_id, data=canonical, media_type="text/plain")
    locator_hash = hashlib.sha256(locator.encode()).hexdigest()
    content_hash = hashlib.sha256(canonical).hexdigest()
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        source_id = conn.execute(
            text(
                "INSERT INTO sources (project_id, kind, locator, canonical_locator_hash, approved_config, status) "
                "VALUES (:project_id, :kind, :locator, :locator_hash, '{}'::jsonb, 'active') "
                "ON CONFLICT (project_id, canonical_locator_hash) DO UPDATE SET version = sources.version + 1 "
                "RETURNING id::text"
            ),
            {"project_id": project_id, "kind": kind, "locator": locator, "locator_hash": locator_hash},
        ).scalar_one()
        version_id = conn.execute(
            text(
                "INSERT INTO source_versions (project_id, source_id, raw_artifact_id, canonical_artifact_id, content_sha256, parser_version, fetched_at) "
                "VALUES (:project_id, :source_id, :raw_id, :text_id, :content_hash, :parser, now()) "
                "ON CONFLICT (project_id, source_id, content_sha256, parser_version) DO UPDATE SET fetched_at = source_versions.fetched_at "
                "RETURNING id::text"
            ),
            {
                "project_id": project_id,
                "source_id": source_id,
                "raw_id": raw_ref["artifact_id"],
                "text_id": text_ref["artifact_id"],
                "content_hash": content_hash,
                "parser": PARSER_VERSION,
            },
        ).scalar_one()
        for chunk in chunks:
            conn.execute(
                text(
                    "INSERT INTO chunks (project_id, source_version_id, ordinal, text_content, start_offset, end_offset, chunker_version) "
                    "VALUES (:project_id, :version_id, :ordinal, :text_content, :start_offset, :end_offset, :chunker) "
                    "ON CONFLICT (project_id, source_version_id, ordinal, chunker_version) DO NOTHING"
                ),
                {
                    "project_id": project_id,
                    "version_id": version_id,
                    "ordinal": chunk["ordinal"],
                    "text_content": chunk["text"],
                    "start_offset": chunk["start_offset"],
                    "end_offset": chunk["end_offset"],
                    "chunker": CHUNKER_VERSION,
                },
            )
    return {"source_id": str(source_id), "source_version_id": str(version_id), "content_sha256": content_hash}
