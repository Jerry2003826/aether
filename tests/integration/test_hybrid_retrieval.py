import fcntl
import time
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.ingest import store_text_version
from aether_worker.retrieval import EMBEDDING_REVISION, RetrievalError, activate_generation, build_generation, encode_texts, group_metrics, release_retired, search
from tests.integration.test_auth import ORIGIN, _bootstrap

APPLE = "苹果派的烘烤温度是一百八十度"
RAIL = "The railway timetable lists the midnight express to York"
EMOJI = "庆祝 😀 完成发布"
CODE = "assert left != right && flag == true"
NEGATION = "不 接受这个结论"
ENGLISH_NOT = "not ready for release"
UNICORN = "独角兽望远镜只在第二代索引出现"


def _owner_and_project(client, key: str, name: str) -> tuple[str, str]:
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    headers = {**ORIGIN, "X-CSRF-Token": body["csrf_token"]}
    project = client.post("/v1/projects", headers={**headers, "Idempotency-Key": key}, json={"name": name})
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"]


def _store(database: Database, store: ArtifactStore, owner_id: str, project_id: str, name: str, body: str) -> None:
    store_text_version(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        kind="local_document",
        locator=f"file://notes/{name}.txt",
        raw=body.encode(),
        text_value=body,
        width=512,
    )


def _chunk_ids(database: Database, owner_id: str, project_id: str) -> dict[str, str]:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text("SELECT text_content, id::text FROM chunks WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).all()
    return {text_value: chunk_id for text_value, chunk_id in rows}


def _ready(database, store, owner_id, project_id, index_root, key: str) -> str:
    generation_id = build_generation(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        generation_key=key,
        index_root=index_root,
        force=True,
    )
    activate_generation(database, owner_id=owner_id, project_id=project_id, generation_id=generation_id)
    return generation_id


def test_measured_revision_and_grouped_channels(client, api_db, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "retrieval-a", "检索")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    for name, body in (("apple", APPLE), ("rail", RAIL), ("emoji", EMOJI), ("code", CODE), ("neg", NEGATION), ("english", ENGLISH_NOT)):
        _store(database, store, owner_id, project_id, name, body)
    _ready(database, store, owner_id, project_id, index_root, "gen-groups")
    ids = _chunk_ids(database, owner_id, project_id)
    vector = encode_texts([APPLE])[0]
    assert len(vector) == 1024
    assert abs(sum(value * value for value in vector) ** 0.5 - 1) < 0.05

    found = search(database, owner_id=owner_id, project_id=project_id, query="苹果派", index_root=index_root)
    assert found["embedding_revision"] == EMBEDDING_REVISION
    assert found["query"] == "苹果派"
    assert found["rewrites"] == []
    assert found["stale"] is False
    assert found["degraded"] is False
    assert found["bm25"][0] == ids[APPLE]
    assert found["dense"][0] == ids[APPLE]
    assert set(found["rerank"]) == set(found["rrf"][:30])
    assert APPLE in found["context"]
    assert ids[APPLE] in {item["chunk_id"] for item in found["citations"]}
    assert found["citations"][0]["content_sha256"]
    assert found["citations"][0]["end_offset"] > found["citations"][0]["start_offset"]
    metrics = group_metrics(
        {"bm25": found["bm25"], "dense": found["dense"], "rrf": found["rrf"], "rerank": found["rerank"]},
        {ids[APPLE]},
    )
    assert metrics["bm25"]["recall_at_10"] == 1
    assert metrics["dense"]["mrr_at_10"] == 1
    assert all(0 <= item["ndcg_at_10"] <= 1 for item in metrics.values())

    samples = []
    for _ in range(20):
        started = time.perf_counter()
        search(database, owner_id=owner_id, project_id=project_id, query="苹果派", index_root=index_root)
        samples.append(time.perf_counter() - started)
    p95 = sorted(samples)[int(0.95 * (len(samples) - 1))]
    assert p95 >= 0


def test_cjk_emoji_and_code_symbols_are_retrievable(client, api_db, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "retrieval-symbols", "符号")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    for name, body in (("apple", APPLE), ("rail", RAIL), ("emoji", EMOJI), ("code", CODE), ("neg", NEGATION), ("english", ENGLISH_NOT)):
        _store(database, store, owner_id, project_id, name, body)
    _ready(database, store, owner_id, project_id, index_root, "gen-symbols")
    ids = _chunk_ids(database, owner_id, project_id)

    def top(query: str) -> str:
        result = search(database, owner_id=owner_id, project_id=project_id, query=query, index_root=index_root)
        assert result["bm25"]
        return result["bm25"][0]

    assert top("苹果派") == ids[APPLE]
    assert top("York") == ids[RAIL]
    assert top("😀") == ids[EMOJI]
    assert top("!=") == ids[CODE]
    assert top("不") == ids[NEGATION]
    assert top("not") == ids[ENGLISH_NOT]
    unknown = search(database, owner_id=owner_id, project_id=project_id, query="zzzznotinvocab", index_root=index_root)
    assert unknown["bm25"] == []


def test_deleted_and_revoked_chunks_are_invisible_before_context(client, api_db, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "retrieval-hide", "隐藏")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    _store(database, store, owner_id, project_id, "apple", APPLE)
    _store(database, store, owner_id, project_id, "code", CODE)
    _ready(database, store, owner_id, project_id, index_root, "gen-hide")
    ids = _chunk_ids(database, owner_id, project_id)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(text("UPDATE sources SET status = 'revoked' WHERE project_id = :project_id AND locator = 'file://notes/apple.txt'"), {"project_id": project_id})
        conn.execute(text("UPDATE chunks SET deleted_at = now() WHERE project_id = :project_id AND id = :id"), {"project_id": project_id, "id": ids[CODE]})
    hidden = search(database, owner_id=owner_id, project_id=project_id, query="苹果派", index_root=index_root)
    removed = search(database, owner_id=owner_id, project_id=project_id, query="!=", index_root=index_root)
    for result, banned, chunk_id in ((hidden, APPLE, ids[APPLE]), (removed, CODE, ids[CODE])):
        assert banned not in result["context"]
        assert chunk_id not in result["bm25"]
        assert chunk_id not in result["dense"]
        assert chunk_id not in result["rrf"]
        assert chunk_id not in result["rerank"]
        assert all(item["chunk_id"] != chunk_id for item in result["citations"])


def test_other_project_cannot_read_the_index(client, api_db, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "retrieval-scope-a", "甲")
    _other_owner, other_project = _owner_and_project(client, "retrieval-scope-b", "乙")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    _store(database, store, owner_id, project_id, "apple", APPLE)
    generation_id = _ready(database, store, owner_id, project_id, index_root, "gen-scope")
    with pytest.raises(RetrievalError) as missing:
        search(database, owner_id=owner_id, project_id=other_project, query="苹果派", index_root=index_root, generation_id=generation_id)
    assert missing.value.code == "generation_missing"
    assert APPLE not in str(missing.value)


def test_generation_swap_marks_retired_search_stale(client, api_db, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "retrieval-swap", "切换")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    _store(database, store, owner_id, project_id, "apple", APPLE)
    first = _ready(database, store, owner_id, project_id, index_root, "gen-first")
    _store(database, store, owner_id, project_id, "unicorn", UNICORN)
    lagging = search(database, owner_id=owner_id, project_id=project_id, query="独角兽", index_root=index_root)
    assert lagging["index_lag"] is True
    assert UNICORN not in lagging["context"]
    assert lagging["bm25"] == []
    second = build_generation(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        generation_key="gen-second",
        index_root=index_root,
        force=True,
    )
    still_old = search(database, owner_id=owner_id, project_id=project_id, query="独角兽", index_root=index_root)
    assert UNICORN not in still_old["context"]
    activate_generation(database, owner_id=owner_id, project_id=project_id, generation_id=second)
    current = search(database, owner_id=owner_id, project_id=project_id, query="独角兽", index_root=index_root)
    assert current["bm25"]
    assert UNICORN in current["context"]
    retired = search(database, owner_id=owner_id, project_id=project_id, query="独角兽", index_root=index_root, generation_id=first)
    assert retired["stale"] is True
    assert retired["context"] == ""
    assert retired["bm25"] == []
    assert retired["rerank"] == []


def test_build_debounce_and_lock(client, api_db, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "retrieval-lock", "锁")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    _store(database, store, owner_id, project_id, "apple", APPLE)
    first = build_generation(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        generation_key="gen-lock",
        index_root=index_root,
        force=True,
    )
    second = build_generation(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        generation_key="gen-lock-again",
        index_root=index_root,
        force=False,
    )
    assert second == first
    with psycopg.connect(role_urls["aether_worker"], autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 11))", (project_id,))
        with pytest.raises(RetrievalError) as busy:
            build_generation(
                database,
                store,
                owner_id=owner_id,
                project_id=project_id,
                generation_key="gen-busy",
                index_root=index_root,
                force=True,
            )
        assert busy.value.code == "build_busy"
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 11))", (project_id,))


def test_retired_directory_stays_until_the_reader_lock_clears(client, api_db, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "retrieval-gc", "清理")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    index_root = tmp_path / "indexes"
    _store(database, store, owner_id, project_id, "apple", APPLE)
    _ready(database, store, owner_id, project_id, index_root, "gen-old")
    _ready(database, store, owner_id, project_id, index_root, "gen-new")
    lock_path = index_root / "gen-old" / ".search.lock"
    handle = lock_path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
    try:
        assert "gen-old" not in release_retired(database, owner_id=owner_id, project_id=project_id, index_root=index_root)
        assert (index_root / "gen-old").is_dir()
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    assert "gen-old" in release_retired(database, owner_id=owner_id, project_id=project_id, index_root=index_root)
    assert not (index_root / "gen-old").exists()
    assert (index_root / "gen-new").is_dir()
