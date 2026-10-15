from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.ingest import store_text_version
from aether_worker.memory import apply_memory_commands, remember
from aether_worker.retention import LIMITS, commit_claim_text, commit_embedding, current_epoch, invalidate_derived
from aether_worker.retrieval import generation_is_fresh
from tests.integration.test_auth import ORIGIN, _bootstrap

BODY = "需要忘掉的烘烤温度记录"


def _session(client, key: str):
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    headers = {**ORIGIN, "X-CSRF-Token": body["csrf_token"], "Idempotency-Key": key}
    project = client.post("/v1/projects", headers=headers, json={"name": "遗忘"})
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"], {**ORIGIN, "X-CSRF-Token": body["csrf_token"]}


def test_forgetting_blocks_late_writes_and_does_not_claim_physical_delete(client, role_urls, tmp_path) -> None:
    owner_id, project_id, headers = _session(client, "forget-project")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    store_text_version(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        kind="local_document",
        locator="file://notes/forget.txt",
        raw=BODY.encode(),
        text_value=BODY,
        width=512,
    )
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        chunk_id = conn.execute(
            text("SELECT id::text FROM chunks WHERE project_id = :project_id AND text_content = :body"),
            {"project_id": project_id, "body": BODY},
        ).scalar_one()
        seen_epoch = current_epoch(conn, project_id)
        manifest = store.put(owner_id=owner_id, project_id=project_id, data=b"{}", media_type="application/json", conn=conn)
        conn.execute(
            text(
                "INSERT INTO index_generations (project_id, generation_key, tokenizer_version, source_watermark, "
                "manifest_artifact_id, state, manifest_sha256) "
                "VALUES (:project_id, 'gen-forget', 'test', 1, :artifact_id, 'ACTIVE', :sha)"
            ),
            {"project_id": project_id, "artifact_id": manifest["artifact_id"], "sha": manifest["sha256"]},
        )
        conn.execute(
            text("UPDATE chunks SET deleted_at = now() WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": chunk_id},
        )
        report = invalidate_derived(conn, project_id=project_id)
    assert report["previous_epoch"] == seen_epoch
    assert report["visibility_epoch"] == seen_epoch + 1
    assert report["retired_generations"] == ["gen-forget"]
    assert report["physical_delete"] is False
    assert list(LIMITS) == report["limits"]
    assert "备份" in report["limits"][1]
    zeros = "[" + ",".join(["0"] * 1024) + "]"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        wrote = commit_embedding(
            conn,
            project_id=project_id,
            chunk_id=chunk_id,
            embedding=zeros,
            revision="late-job",
            seen_epoch=seen_epoch,
        )
        fresh = generation_is_fresh(conn, project_id, seen_epoch, [chunk_id])
        stored = conn.execute(
            text("SELECT embedding IS NULL AS missing, deleted_at IS NOT NULL AS gone FROM chunks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": chunk_id},
        ).mappings().one()
        profile = conn.execute(
            text("SELECT feature_config FROM profile_snapshots WHERE project_id = :project_id ORDER BY profile_version DESC LIMIT 1"),
            {"project_id": project_id},
        ).scalar_one()
        state = conn.execute(
            text("SELECT state FROM index_generations WHERE project_id = :project_id AND generation_key = 'gen-forget'"),
            {"project_id": project_id},
        ).scalar_one()
    assert wrote is False
    assert fresh is False
    assert stored["missing"] is True
    assert stored["gone"] is True
    assert state == "RETIRED"
    config = profile if isinstance(profile, dict) else profile
    assert config["invalidated"] is True
    memory_id = remember(
        database,
        store,
        owner_id=owner_id,
        project_id=project_id,
        claim="发布日：十月",
        kind="semantic",
        category="fact",
    )
    removed = client.delete(
        f"/v1/projects/{project_id}/memories/{memory_id}",
        headers={**headers, "Idempotency-Key": "forget-memory", "If-Match": "1"},
    )
    assert removed.status_code == 202, removed.text
    apply_memory_commands(database, store, owner_id=owner_id, project_id=project_id)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        epoch_after_memory = current_epoch(conn, project_id)
        rewritten = commit_claim_text(
            conn,
            project_id=project_id,
            memory_id=memory_id,
            claim="发布日：被旧任务改写",
            seen_epoch=seen_epoch,
        )
        claim = conn.execute(
            text("SELECT claim, deleted_at IS NOT NULL AS gone FROM memories WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": memory_id},
        ).mappings().one()
    assert epoch_after_memory > report["visibility_epoch"]
    assert rewritten is False
    assert claim["claim"] == "发布日：十月"
    assert claim["gone"] is True
    listed = client.get(f"/v1/projects/{project_id}/memories")
    assert all(item["id"] != memory_id for item in listed.json()["items"])
