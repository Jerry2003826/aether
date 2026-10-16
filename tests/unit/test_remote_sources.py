"""GitHub and arXiv locators stay on the public API unless the fixture switch is open."""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aether_worker.ingest import FIXTURE_ENV, IngestError
from aether_worker.remote_sources import (
    arxiv_request_url,
    arxiv_text,
    fetch_arxiv,
    fetch_github,
    github_request_url,
    github_text,
)


APPLE = "苹果派的烘烤温度是一百八十度"


def test_github_repo_root_requests_the_readme_on_the_official_host() -> None:
    assert github_request_url("https://api.github.com/repos/octocat/Hello-World") == "https://api.github.com/repos/octocat/Hello-World/readme"
    assert github_request_url("https://api.github.com/repos/octocat/Hello-World/contents/README.md") == (
        "https://api.github.com/repos/octocat/Hello-World/contents/README.md"
    )


def test_arxiv_id_uses_the_export_api() -> None:
    assert arxiv_request_url("1706.03762") == "https://export.arxiv.org/api/query?id_list=1706.03762"
    assert arxiv_request_url("arXiv:1706.03762v1") == "https://export.arxiv.org/api/query?id_list=1706.03762v1"


def test_unofficial_hosts_are_rejected_before_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits["n"] += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.delenv(FIXTURE_ENV, raising=False)
    try:
        with pytest.raises(IngestError) as github_blocked:
            fetch_github(f"http://127.0.0.1:{port}/repos/octocat/Hello-World")
        assert github_blocked.value.code == "source_host_rejected"
        with pytest.raises(IngestError) as foreign:
            github_request_url("https://evil.example/repos/octocat/Hello-World")
        assert foreign.value.code == "source_host_rejected"
        with pytest.raises(IngestError) as arxiv_blocked:
            arxiv_request_url(f"http://127.0.0.1:{port}/api/query?id_list=1706.03762")
        assert arxiv_blocked.value.code == "source_host_rejected"
    finally:
        server.shutdown()
    assert hits["n"] == 0


def test_approved_domain_is_required_before_github_or_arxiv_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits["n"] += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setenv(FIXTURE_ENV, "1")
    monkeypatch.setenv("AETHER_INGEST_FIXTURE_HOSTS", "127.0.0.1")
    try:
        with pytest.raises(IngestError) as github_blocked:
            fetch_github(
                f"http://127.0.0.1:{port}/repos/octocat/Hello-World",
                allowed_domains={"example.test"},
            )
        assert github_blocked.value.code == "domain_rejected"
        with pytest.raises(IngestError) as arxiv_blocked:
            fetch_arxiv(
                f"http://127.0.0.1:{port}/api/query?id_list=1706.03762",
                allowed_domains=set(),
            )
        assert arxiv_blocked.value.code == "domain_rejected"
        with pytest.raises(IngestError) as official:
            fetch_github("https://api.github.com/repos/octocat/Hello-World", allowed_domains={"example.test"})
        assert official.value.code == "domain_rejected"
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert hits["n"] == 0


def test_github_json_and_arxiv_atom_become_canonical_text() -> None:
    encoded = base64.b64encode(APPLE.encode()).decode()
    wrapped = "\n".join(encoded[index : index + 60] for index in range(0, len(encoded), 60))
    assert github_text(f'{{"encoding":"base64","content":"{wrapped}"}}'.encode()) == APPLE
    atom = (
        "<feed xmlns='http://www.w3.org/2005/Atom'><entry>"
        "<title>  </title><summary>The railway timetable lists the midnight express to York</summary>"
        "<author><name>Ada</name></author></entry></feed>"
    ).encode()
    assert arxiv_text(atom) == "The railway timetable lists the midnight express to York\nAda"
