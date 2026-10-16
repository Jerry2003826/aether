"""Public GitHub and arXiv readers. Unofficial hosts never open a socket."""

from __future__ import annotations

import base64
import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aether_worker.ingest import IngestError, _parse_rss_subprocess, fetch_url, fixtures_enabled

GITHUB_API_HOST = "api.github.com"
ARXIV_API_HOST = "export.arxiv.org"
FIXTURE_HOSTS_ENV = "AETHER_INGEST_FIXTURE_HOSTS"
_OWNER_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_ARXIV_ID = re.compile(r"^(?:arXiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE)
_GITHUB_HEADERS = {"User-Agent": "aether-ingest", "Accept": "application/vnd.github+json"}
_ARXIV_HEADERS = {"User-Agent": "aether-ingest", "Accept": "application/atom+xml"}


def fixture_allow_hosts() -> set[str]:
    """Loopback stand-ins are honored only while the fixture switch is open."""
    if not fixtures_enabled():
        return set()
    raw = os.environ.get(FIXTURE_HOSTS_ENV, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def github_request_url(locator: str) -> str:
    parsed = _public_parts(locator, GITHUB_API_HOST)
    if not _single_query(parsed.query, "ref"):
        raise IngestError("source_host_rejected")
    path = _github_path(parsed.path)
    return _rebuild(parsed, path, parsed.query)


def fetch_github(locator: str) -> bytes:
    url = github_request_url(locator)
    return fetch_url(url, allow_hosts=_allow_for(url, GITHUB_API_HOST), redirects_left=0, extra_headers=_GITHUB_HEADERS)


def github_text(raw: bytes) -> str:
    payload = _load_github_json(raw)
    if not isinstance(payload, dict):
        raise IngestError("parse_rejected")
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise IngestError("parse_rejected")
    content = str(payload["content"])
    if content.strip() == "":
        raise IngestError("parse_rejected")
    try:
        decoded = base64.b64decode(content, validate=False)
        text_value = decoded.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise IngestError("parse_rejected") from exc
    if text_value.strip() == "":
        raise IngestError("empty_body")
    return text_value


def arxiv_request_url(locator: str) -> str:
    raw = locator.strip()
    matched = _ARXIV_ID.match(raw)
    if matched:
        return f"https://{ARXIV_API_HOST}/api/query?id_list={matched.group(1)}"
    parsed = _public_parts(raw, ARXIV_API_HOST)
    if not _single_query(parsed.query, "id_list"):
        raise IngestError("source_host_rejected")
    if parsed.path.rstrip("/") != "/api/query":
        raise IngestError("source_host_rejected")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    identifiers = [value for key, value in pairs if key == "id_list" and value]
    if len(pairs) != 1 or len(identifiers) != 1:
        raise IngestError("source_host_rejected")
    return _rebuild(parsed, "/api/query", urlencode([("id_list", identifiers[0])]))


def fetch_arxiv(locator: str) -> bytes:
    url = arxiv_request_url(locator)
    return fetch_url(url, allow_hosts=_allow_for(url, ARXIV_API_HOST), redirects_left=0, extra_headers=_ARXIV_HEADERS)


def arxiv_text(raw: bytes) -> str:
    pieces = _parse_rss_subprocess(raw, spin=False, mode="arxiv")
    text_value = "\n".join(pieces)
    if text_value.strip() == "":
        raise IngestError("empty_body")
    return text_value


def _allow_for(url: str, official: str) -> set[str] | None:
    host = urlsplit(url).hostname or ""
    if host == official:
        return None
    return fixture_allow_hosts()


def _public_parts(locator: str, official: str):
    parsed = urlsplit(locator.strip())
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not host:
        raise IngestError("source_host_rejected")
    if host != official and host not in fixture_allow_hosts():
        raise IngestError("source_host_rejected")
    return parsed


def _single_query(query: str, name: str) -> bool:
    if query == "":
        return name == "ref"
    pairs = parse_qsl(query, keep_blank_values=True)
    return len(pairs) == 1 and pairs[0][0] == name and bool(pairs[0][1])


def _github_path(path: str) -> str:
    parts = [item for item in path.split("/") if item]
    if any(item in {".", ".."} for item in parts):
        raise IngestError("source_host_rejected")
    if len(parts) < 3 or parts[0] != "repos" or not _OWNER_REPO.match(parts[1]) or not _OWNER_REPO.match(parts[2]):
        raise IngestError("source_host_rejected")
    owner, repo = parts[1], parts[2]
    if len(parts) == 3:
        return f"/repos/{owner}/{repo}/readme"
    if len(parts) == 4 and parts[3] == "readme":
        return f"/repos/{owner}/{repo}/readme"
    if len(parts) >= 5 and parts[3] == "contents":
        return "/repos/" + "/".join([owner, repo, "contents", *parts[4:]])
    raise IngestError("source_host_rejected")


def _rebuild(parsed, path: str, query: str) -> str:
    host = parsed.hostname or ""
    netloc = host if parsed.port in {None, 80, 443} else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, path, query, ""))


def _load_github_json(raw: bytes) -> object:
    try:
        text_value = raw.decode("utf-8")
    except UnicodeError as exc:
        raise IngestError("parse_rejected") from exc
    try:
        return json.loads(text_value)
    except json.JSONDecodeError:
        try:
            return json.loads(text_value.replace("\r\n", "\n").replace("\n", "\\n"))
        except json.JSONDecodeError as exc:
            raise IngestError("parse_rejected") from exc

