"""Public GitHub and arXiv ingestion, visibility epoch, and read-only recommendations."""

from __future__ import annotations

import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.console_commands import apply_console_commands
from aether_worker.ingest import FIXTURE_ENV, store_text_version
from aether_worker.retention import current_epoch, invalidate_derived, read_cached_result
from aether_worker.retrieval import activate_generation, build_generation
from aether_worker.service import route_pending
from tests.integration.test_console_api import _headers, _session
from tests.integration.test_hybrid_retrieval import APPLE, RAIL

FIXTURE_HOSTS = "AETHER_INGEST_FIXTURE_HOSTS"


def _worker_pass(role_urls: dict[str, str], store: ArtifactStore, seen: dict[str, str]) -> None:
    route_pending(
        Database(role_urls["aether_dispatcher"]),
        Database(role_urls["aether_worker"]),
        Database(role_urls["aether_verifier"]),
        store,
        seen,
    )


def test_github_and_arxiv_are_fetched_indexed_and_searchable(client, role_urls, monkeypatch, tmp_path: Path) -> None:
    hits: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits[self.path] = self.headers.get("User-Agent", "")
            if self.path == "/repos/octocat/Hello-World/readme":
                body = json.dumps({"encoding": "base64", "content": base64.b64encode(APPLE.encode()).decode()}).encode()
            elif self.path.startswith("/api/query"):
                body = (
                    "<feed xmlns='http://www.w3.org/2005/Atom'><entry><title> </title>"
                    f"<summary>{RAIL}</summary></entry></feed>"
                ).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setenv(FIXTURE_ENV, "1")
    monkeypatch.setenv(FIXTURE_HOSTS, "127.0.0.1")
    monkeypatch.setenv("AETHER_INDEX_ROOT", str(client.app.state.settings.index_root))
    owner_id, project_id, csrf = _session(client, "r7-remote-project")
    store = ArtifactStore(client.app.state.settings.artifact_root, Database(role_urls["aether_worker"]))
    try:
        github = client.post(
            f"/v1/projects/{project_id}/sources",
            headers=_headers(csrf, "r7-github-source"),
            json={
                "kind": "github_api",
                "locator": f"http://127.0.0.1:{port}/repos/octocat/Hello-World",
                "allowed_domains": ["127.0.0.1"],
            },
        )
        arxiv = client.post(
            f"/v1/projects/{project_id}/sources",
            headers=_headers(csrf, "r7-arxiv-source"),
            json={
                "kind": "arxiv_api",
                "locator": f"http://127.0.0.1:{port}/api/query?id_list=1706.03762",
                "allowed_domains": ["127.0.0.1"],
            },
        )
        assert github.status_code == 202, github.text
        assert arxiv.status_code == 202, arxiv.text
        seen: dict[str, str] = {}
        _worker_pass(role_urls, store, seen)
        listed = client.get(f"/v1/projects/{project_id}/sources").json()["items"]
        by_kind = {item["kind"]: item for item in listed}
        for kind, key in (("github_api", "r7-github-job"), ("arxiv_api", "r7-arxiv-job")):
            created = client.post(
                f"/v1/projects/{project_id}/ingestions",
                headers=_headers(csrf, key),
                json={"source_id": by_kind[kind]["id"]},
            )
            assert created.status_code == 202, created.text
            assert created.json()["status"] == "accepted"
        _worker_pass(role_urls, store, seen)
        jobs = client.get(f"/v1/projects/{project_id}/ingestions").json()["items"]
        assert [item["status"] for item in jobs] == ["SUCCEEDED", "SUCCEEDED"]
        assert all(item["error_code"] is None and item["source_version_id"] for item in jobs)
        apple = client.post(
            f"/v1/projects/{project_id}/search",
            headers=_headers(csrf, "r7-search-apple"),
            json={"project_id": project_id, "query": "苹果派", "mode": "hybrid"},
        )
        rail = client.post(
            f"/v1/projects/{project_id}/search",
            headers=_headers(csrf, "r7-search-rail"),
            json={"project_id": project_id, "query": "York", "mode": "hybrid"},
        )
    finally:
        server.shutdown()
    assert hits["/repos/octocat/Hello-World/readme"].startswith("aether-ingest")
    assert "/api/query?id_list=1706.03762" in hits
    assert apple.status_code == 200, apple.text
    assert rail.status_code == 200, rail.text
    assert apple.json()["hits"][0]["text"] == APPLE
    assert rail.json()["hits"][0]["text"] == RAIL
    with Database(role_urls["aether_worker"]).transaction(owner_id=owner_id, project_id=project_id) as conn:
        embedded = conn.execute(
            text("SELECT count(*) FROM chunks WHERE project_id = :project_id AND embedding_revision IS NOT NULL AND deleted_at IS NULL"),
            {"project_id": project_id},
        ).scalar_one()
    assert int(embedded) >= 2


def test_deleting_a_source_advances_the_epoch_and_drops_the_search_cache(client, role_urls, tmp_path: Path) -> None:
    owner_id, project_id, csrf = _session(client, "r7-epoch-project")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = client.app.state.settings.index_root
    apple = store_text_version(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        kind="local_document",
        locator="file://notes/apple.txt",
        raw=APPLE.encode(),
        text_value=APPLE,
        width=512,
    )
    store_text_version(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        kind="local_document",
        locator="file://notes/rail.txt",
        raw=RAIL.encode(),
        text_value=RAIL,
        width=512,
    )
    generation_id = build_generation(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        generation_key="gen-r7-epoch",
        index_root=index_root,
        force=True,
    )
    activate_generation(database, owner_id=owner_id, project_id=project_id, generation_id=generation_id)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        epoch_before = current_epoch(conn, project_id)
    found = client.post(
        f"/v1/projects/{project_id}/search",
        headers=_headers(csrf, "r7-epoch-search"),
        json={"project_id": project_id, "query": "苹果派", "mode": "hybrid"},
    )
    assert found.status_code == 200, found.text
    assert found.json()["hits"][0]["text"] == APPLE
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        cached = read_cached_result(conn, project_id, "苹果派", generation_id=generation_id)
    assert cached is not None
    assert APPLE in str(cached)
    removed = client.delete(
        f"/v1/projects/{project_id}/sources/{apple['source_id']}",
        headers=_headers(csrf, "r7-epoch-delete", "1"),
    )
    assert removed.status_code == 202, removed.text
    apply_console_commands(database, owner_id=owner_id, project_id=project_id)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        epoch_after = current_epoch(conn, project_id)
        cached_after = read_cached_result(conn, project_id, "苹果派", generation_id=generation_id)
        gone = conn.execute(
            text("SELECT deleted_at IS NOT NULL FROM chunks WHERE project_id = :project_id AND text_content = :body"),
            {"project_id": project_id, "body": APPLE},
        ).scalar_one()
    assert epoch_after == epoch_before + 1
    assert cached_after is None
    assert gone is True
    again = client.post(
        f"/v1/projects/{project_id}/search",
        headers=_headers(csrf, "r7-epoch-search-again"),
        json={"project_id": project_id, "query": "苹果派", "mode": "hybrid"},
    )
    kept = client.post(
        f"/v1/projects/{project_id}/search",
        headers=_headers(csrf, "r7-epoch-search-rail"),
        json={"project_id": project_id, "query": "York", "mode": "hybrid"},
    )
    assert again.status_code == 200, again.text
    assert all(APPLE not in hit["text"] for hit in again.json()["hits"])
    assert kept.status_code == 200, kept.text
    assert kept.json()["hits"][0]["text"] == RAIL


def test_recommendation_get_does_not_write_profile_or_impression(client, role_urls) -> None:
    owner_id, project_id, csrf = _session(client, "r7-reco-read")
    created = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=_headers(csrf, "r7-reco-source"),
        json={"kind": "local_document", "locator": "只读推荐"},
    )
    assert created.status_code == 202, created.text
    database = Database(role_urls["aether_worker"])
    apply_console_commands(database, owner_id=owner_id, project_id=project_id)
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def read_recommendations() -> None:
        barrier.wait(timeout=5)
        for _ in range(6):
            response = client.get(f"/v1/projects/{project_id}/recommendations")
            if response.status_code != 200:
                errors.append(response.text)
                return

    def forget() -> None:
        barrier.wait(timeout=5)
        try:
            with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
                invalidate_derived(conn, project_id=project_id)
        except Exception as exc:
            errors.append(type(exc).__name__)

    readers = threading.Thread(target=read_recommendations)
    writers = threading.Thread(target=forget)
    readers.start()
    writers.start()
    readers.join(timeout=15)
    writers.join(timeout=15)
    assert errors == []
    recommended = client.get(f"/v1/projects/{project_id}/recommendations")
    assert recommended.status_code == 200, recommended.text
    body = recommended.json()
    assert body["impression_id"] is None
    assert body["items"][0]["title"] == "只读推荐"
    assert body["items"][0]["exploration"] is False
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        impressions = conn.execute(
            text("SELECT count(*) FROM recommendation_impressions WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one()
        profiles = conn.execute(
            text("SELECT feature_config FROM profile_snapshots WHERE project_id = :project_id ORDER BY profile_version"),
            {"project_id": project_id},
        ).scalars().all()
    assert int(impressions) == 0
    assert len(profiles) == 1
    config = profiles[0] if isinstance(profiles[0], dict) else json.loads(profiles[0])
    assert config["reason"] == "visibility_epoch"
    feedback = client.post(
        f"/v1/projects/{project_id}/feedback",
        headers=_headers(csrf, "r7-reco-feedback"),
        json={
            "event_id": str(uuid.uuid4()),
            "source_id": body["items"][0]["source_id"],
            "kind": "helpful",
            "event_time": "2026-10-16T00:00:00Z",
        },
    )
    assert feedback.status_code == 202, feedback.text
