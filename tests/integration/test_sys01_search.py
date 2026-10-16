"""Public search must read an ACTIVE index. Setup writes the index; the assertion is the HTTP route."""

from __future__ import annotations

import uuid
from pathlib import Path

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from tests.integration.test_auth import ORIGIN
from tests.integration.test_console_api import _headers, _session
from tests.integration.test_hybrid_retrieval import APPLE, _ready, _store
from tests.integration.test_worker_process import ROOT

_ML = ROOT / "environments" / "ml"


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


def test_lexical_search_ignores_a_missing_embedding_process(client, role_urls, tmp_path: Path) -> None:
    owner_id, project_id, csrf = _session(client, "sys01-lexical-project")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    _store(database, store, owner_id, project_id, "apple", APPLE)
    _ready(database, store, owner_id, project_id, index_root, "gen-lexical")
    hidden = _hide_ml_scripts()
    try:
        lexical = client.post(
            f"/v1/projects/{project_id}/search",
            headers={**ORIGIN, "X-CSRF-Token": csrf},
            json={"project_id": project_id, "query": "烘烤", "mode": "lexical"},
        )
        assert lexical.status_code == 200, lexical.text
        body = lexical.json()
        assert body["degraded"] is False
        assert body["hits"][0]["text"] == APPLE
        hybrid = client.post(
            f"/v1/projects/{project_id}/search",
            headers={**ORIGIN, "X-CSRF-Token": csrf},
            json={"project_id": project_id, "query": "烘烤", "mode": "hybrid"},
        )
        assert hybrid.status_code == 503, hybrid.text
        assert hybrid.json()["code"] == "index_unavailable"
    finally:
        _restore_ml_scripts(hidden)


def test_source_filter_keeps_chunks_outside_the_global_window(client, role_urls, tmp_path: Path) -> None:
    owner_id, project_id, csrf = _session(client, "sys01-window-project")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    for ordinal in range(55):
        _store(database, store, owner_id, project_id, f"apple-{ordinal}", APPLE)
    _ready(database, store, owner_id, project_id, index_root, "gen-window")
    headers = {**ORIGIN, "X-CSRF-Token": csrf}
    listed = client.get(f"/v1/projects/{project_id}/sources?limit=100")
    assert listed.status_code == 200, listed.text
    by_version = {item["latest_source_version_id"]: item["id"] for item in listed.json()["items"]}
    assert len(by_version) == 55

    def documents(mode: str, source_id: str | None = None) -> list[str]:
        payload: dict[str, object] = {"project_id": project_id, "query": "苹果派", "mode": mode, "limit": 50}
        if source_id is not None:
            payload["source_ids"] = [source_id]
            payload["limit"] = 8
        response = client.post(f"/v1/projects/{project_id}/search", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        return [hit["document_id"] for hit in response.json()["hits"]]

    lexical_ids = set(documents("lexical"))
    dense_ids = set(documents("dense"))
    hybrid_ids = set(documents("hybrid"))
    lexical_missing = [version for version in by_version if version not in lexical_ids and version not in hybrid_ids]
    dense_missing = [version for version in by_version if version not in dense_ids]
    assert lexical_missing, "词法窗口没有露出被截断的片段"
    assert dense_missing, "向量窗口没有露出被截断的片段"
    lexical_version = lexical_missing[0]
    dense_version = dense_missing[0]
    lexical_source = by_version[lexical_version]
    dense_source = by_version[dense_version]

    def filtered(mode: str, source_id: str, version_id: str) -> None:
        payload = {
            "project_id": project_id,
            "query": "苹果派",
            "mode": mode,
            "limit": 8,
            "source_ids": [source_id],
        }
        response = client.post(f"/v1/projects/{project_id}/search", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        hits = response.json()["hits"]
        assert hits, response.text
        assert hits[0]["document_id"] == version_id
        assert hits[0]["text"] == APPLE

    filtered("lexical", lexical_source, lexical_version)
    filtered("hybrid", lexical_source, lexical_version)
    filtered("dense", dense_source, dense_version)


def _hide_ml_scripts() -> list[tuple[Path, Path]]:
    hidden: list[tuple[Path, Path]] = []
    for name in ("encode_texts.py", "rerank_pairs.py"):
        source = _ML / name
        target = source.with_suffix(".py.missing")
        source.rename(target)
        hidden.append((target, source))
    return hidden


def _restore_ml_scripts(hidden: list[tuple[Path, Path]]) -> None:
    for target, source in hidden:
        if target.exists():
            target.rename(source)
