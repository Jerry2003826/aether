"""Authorized ingestion. Empty text is a failure. Embeddings stay unset until measured."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpcore
import httpx
import tenacity
from httpx._config import create_ssl_context
from langchain_text_splitters import CharacterTextSplitter
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database

PARSER_VERSION = "text-1"
TEXT_MEDIA = "text/plain"
CHUNKER_VERSION = "codepoint-1"
MAX_HTTP_BYTES = 10 * 1024 * 1024
FIXTURE_ENV = "AETHER_INGEST_ALLOW_FIXTURES"
_REDIRECTS = {301, 302, 303, 307, 308}
_RETRY_STATUSES = {429, 503}
_MAX_FETCH_ATTEMPTS = 3
_MAX_RETRY_AFTER = 8.0


class IngestError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_text(raw: str) -> str:
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def chunk_text(raw: str, *, width: int = 32) -> list[dict[str, int | str]]:
    """Code-point windows. langchain measures length with ``len``, which is code points."""
    text_value = canonical_text(raw)
    if text_value.strip() == "":
        raise IngestError("empty_body")
    if width < 1:
        raise IngestError("invalid_chunk_width")
    parts = _split_code_points(text_value, width)
    chunks: list[dict[str, int | str]] = []
    cursor = 0
    for piece in parts:
        start = text_value.find(piece, cursor)
        if start < 0:
            raise IngestError("invalid_chunk_width")
        end = start + len(piece)
        chunks.append({"ordinal": len(chunks), "text": piece, "start_offset": start, "end_offset": end})
        cursor = end
    if not chunks or cursor != len(text_value):
        raise IngestError("invalid_chunk_width")
    return chunks


def _split_code_points(text_value: str, width: int) -> list[str]:
    splitter = CharacterTextSplitter(
        separator="",
        chunk_size=width,
        chunk_overlap=0,
        strip_whitespace=False,
    )
    return splitter.split_text(text_value)


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


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


def _require_domain(url: str, allowed_domains: set[str]) -> None:
    host = _hostname(url)
    permitted = {item.strip().rstrip(".").lower() for item in allowed_domains if item.strip()}
    if host == "" or host not in permitted:
        raise IngestError("domain_rejected")


def _connect_hosts(allow_hosts: set[str] | None, allowed_domains: set[str] | None) -> set[str] | None:
    if allow_hosts is not None:
        return allow_hosts
    if allowed_domains is None or not fixtures_enabled():
        return None
    return {item.strip().rstrip(".").lower() for item in allowed_domains if item.strip()}


class _RetryableFetch(Exception):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("retryable_fetch")
        self.retry_after = retry_after


def _retry_wait(retry_state: tenacity.RetryCallState) -> float:
    outcome = retry_state.outcome
    exc = None if outcome is None else outcome.exception()
    if isinstance(exc, _RetryableFetch) and exc.retry_after is not None:
        return min(max(exc.retry_after, 0.0), _MAX_RETRY_AFTER)
    return tenacity.wait_exponential(multiplier=0.2, min=0.2, max=2.0)(retry_state)


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def fetch_url(
    url: str,
    *,
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]] = resolve_host,
    allow_hosts: set[str] | None = None,
    allowed_domains: set[str] | None = None,
    opener: Callable[..., httpx.Response] | None = None,
    redirects_left: int = 2,
    max_bytes: int = MAX_HTTP_BYTES,
    verify: ssl.SSLContext | str | bool = True,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    if redirects_left < 0:
        raise IngestError("redirect_rejected")
    if allowed_domains is not None:
        _require_domain(url, allowed_domains)
    try:
        return _fetch_attempt(
            url,
            resolve=resolve,
            allow_hosts=allow_hosts,
            allowed_domains=allowed_domains,
            opener=opener,
            redirects_left=redirects_left,
            max_bytes=max_bytes,
            verify=verify,
            extra_headers=extra_headers,
        )
    except _RetryableFetch as exc:
        raise IngestError("fetch_failed") from exc


@tenacity.retry(
    retry=tenacity.retry_if_exception_type((_RetryableFetch, httpx.TransportError)),
    stop=tenacity.stop_after_attempt(_MAX_FETCH_ATTEMPTS),
    wait=_retry_wait,
    reraise=True,
)
def _fetch_attempt(
    url: str,
    *,
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]],
    allow_hosts: set[str] | None,
    allowed_domains: set[str] | None,
    opener: Callable[..., httpx.Response] | None,
    redirects_left: int,
    max_bytes: int,
    verify: ssl.SSLContext | str | bool,
    extra_headers: dict[str, str] | None,
) -> bytes:
    pinned = assert_public_target(url, resolve=resolve, allow_hosts=_connect_hosts(allow_hosts, allowed_domains))
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    headers = dict(extra_headers or {})
    headers["Host"] = host
    return _fetch_once(
        url,
        resolve=resolve,
        allow_hosts=allow_hosts,
        allowed_domains=allowed_domains,
        opener=opener,
        redirects_left=redirects_left,
        max_bytes=max_bytes,
        verify=verify,
        extra_headers=extra_headers,
        pinned=pinned,
        headers=headers,
    )


def _fetch_once(
    url: str,
    *,
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]],
    allow_hosts: set[str] | None,
    allowed_domains: set[str] | None,
    opener: Callable[..., httpx.Response] | None,
    redirects_left: int,
    max_bytes: int,
    verify: ssl.SSLContext | str | bool,
    extra_headers: dict[str, str] | None,
    pinned: str | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """One hop. Redirects stay inside the caller's single Tenacity budget."""
    if redirects_left < 0:
        raise IngestError("redirect_rejected")
    if allowed_domains is not None:
        _require_domain(url, allowed_domains)
    if pinned is None or headers is None:
        pinned = assert_public_target(url, resolve=resolve, allow_hosts=_connect_hosts(allow_hosts, allowed_domains))
        parsed = urlsplit(url)
        headers = dict(extra_headers or {})
        headers["Host"] = parsed.hostname or ""
    if opener is not None:
        return _opener_body(
            opener,
            url,
            headers=headers,
            resolve=resolve,
            allow_hosts=allow_hosts,
            allowed_domains=allowed_domains,
            redirects_left=redirects_left,
            max_bytes=max_bytes,
            verify=verify,
            extra_headers=extra_headers,
        )
    transport = _PinnedTransport(pinned, verify=verify)
    with httpx.Client(transport=transport, verify=verify, follow_redirects=False, trust_env=False, timeout=5.0) as client:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code in _REDIRECTS:
                return _fetch_once(
                    _location(response, url),
                    resolve=resolve,
                    allow_hosts=allow_hosts,
                    allowed_domains=allowed_domains,
                    opener=opener,
                    redirects_left=redirects_left - 1,
                    max_bytes=max_bytes,
                    verify=verify,
                    extra_headers=extra_headers,
                )
            if response.status_code in _RETRY_STATUSES:
                raise _RetryableFetch(_retry_after(response.headers))
            return _read_limited(response, max_bytes)


def _opener_body(
    opener: Callable[..., httpx.Response],
    url: str,
    *,
    headers: dict[str, str],
    resolve: Callable[[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address]],
    allow_hosts: set[str] | None,
    allowed_domains: set[str] | None,
    redirects_left: int,
    max_bytes: int,
    verify: ssl.SSLContext | str | bool,
    extra_headers: dict[str, str] | None,
) -> bytes:
    response = opener(url, headers=headers)
    if response.status_code in _REDIRECTS:
        return _follow(
            response,
            url,
            resolve=resolve,
            allow_hosts=allow_hosts,
            allowed_domains=allowed_domains,
            opener=opener,
            redirects_left=redirects_left,
            max_bytes=max_bytes,
            verify=verify,
            extra_headers=extra_headers,
        )
    if response.status_code in _RETRY_STATUSES:
        raise _RetryableFetch(_retry_after(response.headers))
    if len(response.content) > max_bytes:
        raise IngestError("size_rejected")
    return response.content


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
    allowed_domains: set[str] | None,
    opener: Callable[..., httpx.Response] | None,
    redirects_left: int,
    max_bytes: int,
    verify: ssl.SSLContext | str | bool,
    extra_headers: dict[str, str] | None,
) -> bytes:
    return _fetch_once(
        _location(response, url),
        resolve=resolve,
        allow_hosts=allow_hosts,
        allowed_domains=allowed_domains,
        opener=opener,
        redirects_left=redirects_left - 1,
        max_bytes=max_bytes,
        verify=verify,
        extra_headers=extra_headers,
    )


def parse_rss_items(xml_bytes: bytes) -> list[str]:
    return _parse_rss_subprocess(xml_bytes, spin=False)


def _parse_rss_subprocess(xml_bytes: bytes, *, spin: bool, mode: str = "rss") -> list[str]:
    env = os.environ.copy()
    worker_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = worker_root + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("AETHER_PARSER_SPIN", None)
    env.pop("AETHER_FEED_MODE", None)
    if spin:
        env["AETHER_PARSER_SPIN"] = "1"
    if mode == "arxiv":
        env["AETHER_FEED_MODE"] = "arxiv"
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


def _insert_version(
    conn: Any,
    *,
    project_id: str,
    source_id: str,
    raw_id: str,
    text_id: str,
    content_hash: str,
    chunks: list[dict[str, Any]],
) -> str:
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
            "raw_id": raw_id,
            "text_id": text_id,
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
    return str(version_id)


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
    raw_ref = store.put(owner_id=owner_id, project_id=project_id, data=raw, media_type=TEXT_MEDIA)
    text_ref = store.put(owner_id=owner_id, project_id=project_id, data=canonical, media_type=TEXT_MEDIA)
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
        version_id = _insert_version(
            conn,
            project_id=project_id,
            source_id=str(source_id),
            raw_id=str(raw_ref["artifact_id"]),
            text_id=str(text_ref["artifact_id"]),
            content_hash=content_hash,
            chunks=chunks,
        )
    return {"source_id": str(source_id), "source_version_id": version_id, "content_sha256": content_hash}


def attach_text_version(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    source_id: str,
    raw: bytes,
    text_value: str,
    width: int = 2000,
    raw_media_type: str = TEXT_MEDIA,
) -> dict[str, str]:
    """Store one version of an existing source. Empty text fails before any write."""
    chunks = chunk_text(text_value, width=width)
    canonical = canonical_text(text_value).encode()
    raw_ref = store.put(owner_id=owner_id, project_id=project_id, data=raw, media_type=raw_media_type)
    text_ref = store.put(owner_id=owner_id, project_id=project_id, data=canonical, media_type=TEXT_MEDIA)
    content_hash = hashlib.sha256(canonical).hexdigest()
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        version_id = _insert_version(
            conn,
            project_id=project_id,
            source_id=source_id,
            raw_id=str(raw_ref["artifact_id"]),
            text_id=str(text_ref["artifact_id"]),
            content_hash=content_hash,
            chunks=chunks,
        )
    return {
        "source_version_id": str(version_id),
        "content_sha256": content_hash,
        "raw_artifact_id": str(raw_ref["artifact_id"]),
        "canonical_artifact_id": str(text_ref["artifact_id"]),
    }
