import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.ingest import IngestError, chunk_text, fetch_url, parse_rss_items, store_text_version
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


def test_fixture_download_is_real_and_parser_stays_static(client, api_db, role_urls, tmp_path) -> None:
    import sys

    assert not any(name.startswith("playwright") for name in sys.modules)
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
