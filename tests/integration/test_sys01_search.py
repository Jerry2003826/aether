"""Public search must read an ACTIVE index. Setup writes the index; the assertion is the HTTP route."""

from __future__ import annotations

import uuid
from pathlib import Path

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from tests.integration.test_auth import ORIGIN
from tests.integration.test_console_api import _headers, _session
from tests.integration.test_hybrid_retrieval import APPLE, _ready, _store


def test_public_search_reads_an_active_index(client, role_urls, tmp_path: Path) -> None:
    owner_id, project_id, csrf = _session(client, "sys01-search-project")
    missing = client.post(
        f"/v1/projects/{project_id}/search",
        headers={**ORIGIN, "X-CSRF-Token": csrf},
        json={"project_id": project_id, "query": "苹果派", "mode": "hybrid"},
    )
    assert missing.status_code == 503
    assert missing.json()["code"] == "index_unavailable"
    assert "还没有可用索引" in missing.json()["message"]

    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    _store(database, store, owner_id, project_id, "apple", APPLE)
    generation_id = _ready(database, store, owner_id, project_id, index_root, "gen-sys01")

    found = client.post(
        f"/v1/projects/{project_id}/search",
        headers={**ORIGIN, "X-CSRF-Token": csrf},
        json={"project_id": project_id, "query": "苹果派", "mode": "hybrid", "limit": 8},
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["index_generation"] == generation_id
    assert body["embedding_revision"]
    assert body["degraded"] is False
    hit = body["hits"][0]
    assert hit["text"] == APPLE
    assert hit["document_id"]
    assert hit["score"] > 0
    assert hit["citation"]["quote"] == APPLE
    assert len(hit["citation"]["source_sha256"]) == 64
    assert hit["citation"]["end"] > hit["citation"]["start"]

    lexical = client.post(
        f"/v1/projects/{project_id}/search",
        headers={**ORIGIN, "X-CSRF-Token": csrf},
        json={"project_id": project_id, "query": "苹果派", "mode": "lexical"},
    )
    assert lexical.status_code == 200, lexical.text
    assert lexical.json()["hits"][0]["text"] == APPLE

    hidden = client.post(
        f"/v1/projects/{project_id}/search",
        headers={**ORIGIN, "X-CSRF-Token": csrf},
        json={"project_id": project_id, "query": "苹果派", "mode": "hybrid", "source_ids": [str(uuid.uuid4())]},
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["hits"] == []

    _owner, other_id, _csrf = _session(client, "sys01-search-other")
    other = client.post(
        f"/v1/projects/{other_id}/search",
        headers=_headers(_csrf, "sys01-search-other-query"),
        json={"project_id": other_id, "query": "苹果派", "mode": "hybrid"},
    )
    assert other.status_code == 503
    assert "还没有可用索引" in other.json()["message"]
