"""Authorized ingestion. Empty text is a failure. Embeddings stay unset until measured."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database

PARSER_VERSION = "text-1"
CHUNKER_VERSION = "codepoint-1"
MAX_HTTP_BYTES = 10 * 1024 * 1024


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


def _blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_private
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


def assert_public_target(url: str, *, resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]] = resolve_host, allow_hosts: set[str] | None = None) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not host:
        raise IngestError("ssrf_rejected")
    if parsed.port not in {None, 80, 443} and host not in (allow_hosts or set()):
        raise IngestError("ssrf_rejected")
    if host in (allow_hosts or set()):
        return host
    addresses = resolve(host)
    if any(_blocked(address) for address in addresses):
        raise IngestError("ssrf_rejected")
    return str(addresses[0])


def _request(url: str, *, headers: dict[str, str]) -> httpx.Response:
    return httpx.get(url, headers=headers, follow_redirects=False, timeout=5.0)


def fetch_url(
    url: str,
    *,
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]] = resolve_host,
    allow_hosts: set[str] | None = None,
    opener: Callable[..., httpx.Response] | None = None,
    redirects_left: int = 2,
    max_bytes: int = MAX_HTTP_BYTES,
) -> bytes:
    if redirects_left < 0:
        raise IngestError("redirect_rejected")
    pinned = assert_public_target(url, resolve=resolve, allow_hosts=allow_hosts)
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if pinned == host:
        target = url
    else:
        host_port = pinned if port in {80, 443} and parsed.port is None else f"{pinned}:{port}"
        target = urlunsplit((parsed.scheme, host_port, parsed.path, parsed.query, ""))
    response = (opener or _request)(target, headers={"Host": host})
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if not location:
            raise IngestError("redirect_rejected")
        if location.startswith("/"):
            location = f"{parsed.scheme}://{parsed.netloc}{location}"
        return fetch_url(location, resolve=resolve, allow_hosts=allow_hosts, opener=opener, redirects_left=redirects_left - 1, max_bytes=max_bytes)
    if len(response.content) > max_bytes:
        raise IngestError("size_rejected")
    return response.content


def parse_rss_items(xml_bytes: bytes) -> list[str]:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise IngestError("parse_rejected") from exc
    texts: list[str] = []
    for node in root.iter():
        if node.tag.endswith("title") or node.tag.endswith("description"):
            if node.text and node.text.strip():
                texts.append(node.text.strip())
    if not texts:
        raise IngestError("empty_body")
    return texts


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
