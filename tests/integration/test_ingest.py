import ipaddress
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.browser import render_controlled_page
from aether_worker.ingest import FIXTURE_ENV, IngestError, _parse_rss_subprocess, chunk_text, fetch_url, parse_rss_items, store_text_version
from tests.integration.test_auth import ORIGIN, _bootstrap


def _project(client):
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    project = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "ingest-project"}, json={"name": "采集"})
    assert project.status_code == 201, project.text
    return session["owner_id"], project.json()["id"]


def test_offsets_cover_cjk_emoji_and_newlines() -> None:
    chunks = chunk_text("甲😀\r\nb", width=2)
    assert [(item["text"], item["start_offset"], item["end_offset"]) for item in chunks] == [("甲😀", 0, 2), ("\nb", 2, 4)]
    canonical = "甲😀\nb"
    for item in chunks:
        assert canonical[item["start_offset"] : item["end_offset"]] == item["text"]
    with pytest.raises(IngestError) as empty:
        chunk_text(" \r\n\t")
    assert empty.value.code == "empty_body"


def test_ssrf_does_not_connect_to_loopback_or_follow_redirect() -> None:
    hits = {"loop": 0, "public": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits["loop"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = {"n": 0}

    def opener(url: str, *, headers: dict[str, str]) -> httpx.Response:
        calls["n"] += 1
        if "127.0.0.1" in url:
            hits["loop"] += 1
        request = httpx.Request("GET", url)
        return httpx.Response(302, headers={"location": f"http://127.0.0.1:{server.server_address[1]}/secret"}, request=request)

    def resolve(host: str):
        if host == "docs.example":
            return [ipaddress.ip_address("93.184.216.34")]
        return [ipaddress.ip_address(host)]

    try:
        with pytest.raises(IngestError) as blocked:
            fetch_url(f"http://127.0.0.1:{server.server_address[1]}/secret", resolve=resolve)
        assert blocked.value.code == "ssrf_rejected"
        assert calls["n"] == 0
        assert hits["loop"] == 0
        with pytest.raises(IngestError) as redirected:
            fetch_url("http://docs.example/start", resolve=resolve, opener=opener)
        assert redirected.value.code == "ssrf_rejected"
        assert calls["n"] == 1
        assert hits["loop"] == 0
    finally:
        server.shutdown()


def test_fixture_download_is_real_and_parser_stays_static(client, api_db, role_urls, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    import aether_worker.ingest as ingest_module

    assert "playwright" not in Path(ingest_module.__file__).read_text(encoding="utf-8")
    owner_id, project_id = _project(client)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = "本地 RSS <item>甲</item>".encode()
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        body = fetch_url(f"http://127.0.0.1:{port}/note", allow_hosts={"127.0.0.1"})
    finally:
        server.shutdown()
    assert "甲" in body.decode()
    rss = "<rss><channel><item><title>标题</title><description>正文</description></item></channel></rss>".encode()
    assert parse_rss_items(rss) == ["标题", "正文"]
    with pytest.raises(IngestError) as empty_rss:
        parse_rss_items(b"<rss><channel></channel></rss>")
    assert empty_rss.value.code == "empty_body"
    with pytest.raises(IngestError) as huge:
        fetch_url(
            "http://docs.example/big",
            resolve=lambda _host: [ipaddress.ip_address("93.184.216.34")],
            opener=lambda url, headers: httpx.Response(200, content=b"x" * 32, request=httpx.Request("GET", url)),
            max_bytes=8,
        )
    assert huge.value.code == "size_rejected"
    store = ArtifactStore(tmp_path / "artifacts", api_db)
    stored = store_text_version(
        Database(role_urls["aether_worker"]),
        store,
        owner_id=owner_id,
        project_id=project_id,
        kind="local_document",
        locator="file://notes/甲.txt",
        raw="甲😀\r\nb".encode(),
        text_value="甲😀\r\nb",
        width=2,
    )
    with Database(role_urls["aether_worker"]).transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text("SELECT text_content, start_offset, end_offset, embedding_revision IS NULL FROM chunks WHERE source_version_id = :id ORDER BY ordinal"),
            {"id": stored["source_version_id"]},
        ).all()
    assert rows[0][0] == "甲😀"
    assert (rows[0][1], rows[0][2]) == (0, 2)
    assert all(item[3] is True for item in rows)


def test_allow_hosts_is_refused_outside_test_configuration(monkeypatch) -> None:
    monkeypatch.delenv(FIXTURE_ENV, raising=False)
    hits = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits["n"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"no")

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(IngestError) as rejected:
            fetch_url(f"http://127.0.0.1:{server.server_address[1]}/x", allow_hosts={"127.0.0.1"})
        assert rejected.value.code == "fixture_hosts_rejected"
        assert hits["n"] == 0
    finally:
        server.shutdown()


def test_stream_disconnects_when_body_exceeds_limit(monkeypatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    sent = {"n": 0}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            try:
                for _ in range(500):
                    self.wfile.write(b"x" * 256)
                    self.wfile.flush()
                    sent["n"] += 256
            except OSError:
                pass
            done.set()

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(IngestError) as rejected:
            fetch_url(f"http://127.0.0.1:{server.server_address[1]}/big", max_bytes=128)
        assert rejected.value.code == "size_rejected"
        assert done.wait(timeout=3)
        assert 0 < sent["n"] < 32_768
    finally:
        server.shutdown()


def test_tls_pins_ip_and_verifies_the_hostname(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=ingest.fixture.example",
            "-addext",
            "subjectAltName=DNS:ingest.fixture.example",
        ],
        check=True,
        capture_output=True,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"tls-body"
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def resolve(host: str):
        if host == "ingest.fixture.example":
            return [ipaddress.ip_address("127.0.0.1")]
        raise AssertionError(host)

    try:
        body = fetch_url(
            f"https://ingest.fixture.example:{port}/note",
            resolve=resolve,
            verify=ssl.create_default_context(cafile=str(cert)),
        )
    finally:
        server.shutdown()
    assert body == b"tls-body"


def test_rss_parser_is_isolated_and_rejects_entities() -> None:
    rss = "<rss><channel><item><title>标题</title></item></channel></rss>".encode()
    assert parse_rss_items(rss) == ["标题"]
    billion = (
        b"<?xml version='1.0'?>"
        b"<!DOCTYPE lolz [<!ENTITY lol 'lol'><!ENTITY lol2 '&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;'>]>"
        b"<rss><channel><title>&lol2;</title></channel></rss>"
    )
    with pytest.raises(IngestError) as entities:
        parse_rss_items(billion)
    assert entities.value.code == "parse_rejected"
    with pytest.raises(IngestError) as spun:
        _parse_rss_subprocess(b"<rss><channel><title>x</title></channel></rss>", spin=True)
    assert spun.value.code == "parse_rejected"


def test_playwright_profile_renders_javascript(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"<html><body>static<script>document.body.textContent='rendered-3'</script></body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        text = render_controlled_page(
            f"http://127.0.0.1:{server.server_address[1]}/",
            user_data_dir=str(tmp_path / "profile"),
        )
    finally:
        server.shutdown()
    assert text == "rendered-3"
