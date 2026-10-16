"""PostgreSQL durability checks for the artifact journal, dedup, and snapshot publish."""

from __future__ import annotations

import io
import stat
import tarfile
import threading
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from aether_storage.artifacts import ArtifactError, ArtifactStore
from tests.integration.test_artifacts import _project


def _store(client: TestClient, api_db) -> ArtifactStore:
    return ArtifactStore(client.app.state.settings.artifact_root, api_db)


def _journal(store: ArtifactStore, project_id: str) -> str:
    path = store.root / "journal" / f"{project_id}.jsonl"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _zip_with(name: str, content: bytes, mode: int | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(name)
        if mode is not None:
            info.external_attr = mode << 16
        archive.writestr(info, content)
    return buffer.getvalue()


def test_committed_journal_is_written_only_after_the_caller_commits(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        stored = store.put(owner_id=owner, project_id=project, data=b"after-commit", media_type="text/plain", conn=conn)
        records = store._journal_rows(project)
        assert not any(row.get("state") == "committed" and row.get("artifact_id") == stored["artifact_id"] for row in records)
    records = store._journal_rows(project)
    assert any(row.get("state") == "committed" and row.get("artifact_id") == stored["artifact_id"] for row in records)
    assert store.get(owner_id=owner, project_id=project, artifact_id=str(stored["artifact_id"])) == (b"after-commit", "text/plain")


def test_outer_rollback_does_not_publish_or_resurrect(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    with pytest.raises(RuntimeError, match="rollback"):
        with api_db.transaction(owner_id=owner, project_id=project) as conn:
            stored = store.put(owner_id=owner, project_id=project, data=b"rolled-back-bytes", media_type="text/plain", conn=conn)
            raise RuntimeError("rollback")
    with pytest.raises(ArtifactError) as missing:
        store.get(owner_id=owner, project_id=project, artifact_id=str(stored["artifact_id"]))
    assert missing.value.code == "not_found"
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        count = conn.execute(text("SELECT count(*) FROM artifacts WHERE project_id = :project_id"), {"project_id": project}).scalar_one()
    assert count == 0
    assert store.recover(owner_id=owner, project_id=project) == []
    frozen = _journal(store, project)
    assert store.recover(owner_id=owner, project_id=project) == []
    assert _journal(store, project) == frozen
    assert any(row.get("state") == "rolled_back" for row in store._journal_rows(project))


def test_commit_rejection_stays_unknown_and_is_not_inserted(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    dialect = api_db.engine.dialect
    original = dialect.do_commit

    def reject(dbapi_connection) -> None:
        raise RuntimeError("commit rejected")

    dialect.do_commit = reject
    try:
        with pytest.raises(RuntimeError, match="commit rejected"):
            store.put(owner_id=owner, project_id=project, data=b"commit-rejected", media_type="text/plain")
    finally:
        dialect.do_commit = original
    rows = store._journal_rows(project)
    assert any(row.get("state") == "unknown" for row in rows)
    assert not any(row.get("state") == "committed" for row in rows)
    assert store.recover(owner_id=owner, project_id=project) == []
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        count = conn.execute(text("SELECT count(*) FROM artifacts WHERE project_id = :project_id"), {"project_id": project}).scalar_one()
    assert count == 0
    frozen = _journal(store, project)
    assert store.recover(owner_id=owner, project_id=project) == []
    assert _journal(store, project) == frozen


def test_commit_ack_loss_reconciles_the_existing_row(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    dialect = api_db.engine.dialect
    original = dialect.do_commit

    def ack_lost(dbapi_connection) -> None:
        dbapi_connection.commit()
        raise RuntimeError("ack lost")

    dialect.do_commit = ack_lost
    try:
        with pytest.raises(RuntimeError, match="ack lost"):
            store.put(owner_id=owner, project_id=project, data=b"ack-lost-bytes", media_type="text/plain")
    finally:
        dialect.do_commit = original
    assert not any(row.get("state") == "committed" for row in store._journal_rows(project))
    assert store.recover(owner_id=owner, project_id=project) == []
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        rows = conn.execute(
            text("SELECT id::text, sha256 FROM artifacts WHERE project_id = :project_id AND deleted_at IS NULL"),
            {"project_id": project},
        ).mappings().all()
    assert len(rows) == 1
    assert store.get(owner_id=owner, project_id=project, artifact_id=str(rows[0]["id"])) == (b"ack-lost-bytes", "text/plain")
    frozen = _journal(store, project)
    assert store.recover(owner_id=owner, project_id=project) == []
    assert _journal(store, project) == frozen


def test_crash_before_insert_is_reconciled_without_publishing(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)

    def explode(*args, **kwargs):
        raise SystemExit(86)

    store._insert_on = explode
    with pytest.raises(SystemExit):
        store.put(owner_id=owner, project_id=project, data=b"crashed-before-insert", media_type="text/plain")
    assert store.recover(owner_id=owner, project_id=project) == []
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        count = conn.execute(text("SELECT count(*) FROM artifacts WHERE project_id = :project_id"), {"project_id": project}).scalar_one()
    assert count == 0
    frozen = _journal(store, project)
    assert any(row.get("state") == "reconciled" for row in store._journal_rows(project))
    assert store.recover(owner_id=owner, project_id=project) == []
    assert _journal(store, project) == frozen


def test_stale_committed_journal_is_not_treated_as_a_live_artifact(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    ghost = str(uuid.uuid4())
    digest = "ab" * 32
    store._journal(
        project,
        {
            "state": "prepared",
            "artifact_id": ghost,
            "storage_key": f"{project}/ab/{digest}",
            "sha256": digest,
            "byte_size": 4,
            "media_type": "text/plain",
            "classification": "project_private",
        },
    )
    store._journal(project, {"state": "committed", "artifact_id": ghost, "sha256": digest})
    assert store.recover(owner_id=owner, project_id=project) == []
    with pytest.raises(ArtifactError) as missing:
        store.get(owner_id=owner, project_id=project, artifact_id=ghost)
    assert missing.value.code == "not_found"
    frozen = _journal(store, project)
    assert store.recover(owner_id=owner, project_id=project) == []
    assert _journal(store, project) == frozen


def test_tombstone_blocks_republish_and_recovery(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    stored = store.put(owner_id=owner, project_id=project, data=b"tombstone-bytes", media_type="text/plain")
    store.tombstone(owner_id=owner, project_id=project, artifact_id=str(stored["artifact_id"]))
    again = store.put(owner_id=owner, project_id=project, data=b"tombstone-bytes", media_type="text/plain")
    assert again["artifact_id"] != stored["artifact_id"]
    with pytest.raises(ArtifactError) as missing:
        store.get(owner_id=owner, project_id=project, artifact_id=str(stored["artifact_id"]))
    assert missing.value.code == "not_found"
    readable, media = store.get(owner_id=owner, project_id=project, artifact_id=str(again["artifact_id"]))
    assert readable == b"tombstone-bytes"
    assert media == "text/plain"
    assert store.recover(owner_id=owner, project_id=project) == []
    frozen = _journal(store, project)
    assert store.recover(owner_id=owner, project_id=project) == []
    assert _journal(store, project) == frozen


def test_concurrent_puts_of_the_same_bytes_return_one_row(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    barrier = threading.Barrier(4)
    found: list[str] = []
    errors: list[BaseException] = []

    def upload() -> None:
        try:
            barrier.wait()
            stored = store.put(owner_id=owner, project_id=project, data=b"concurrent-bytes", media_type="text/plain")
            found.append(str(stored["artifact_id"]))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=upload) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(found)) == 1
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        count = conn.execute(text("SELECT count(*) FROM artifacts WHERE project_id = :project_id"), {"project_id": project}).scalar_one()
    assert count == 1
    assert store.recover(owner_id=owner, project_id=project) == []


def test_distinct_idempotency_keys_for_the_same_bytes_share_one_artifact(client: TestClient, api_db) -> None:
    owner, project, headers = _project(client)
    payload = b"same-bytes-two-keys"
    url = f"/v1/projects/{project}/artifacts"
    first = client.post(url, headers={**headers, "Idempotency-Key": "audit-key-one"}, files={"file": ("a.txt", payload, "text/plain")})
    second = client.post(url, headers={**headers, "Idempotency-Key": "audit-key-two"}, files={"file": ("b.txt", payload, "text/plain")})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["artifact_id"] == second.json()["artifact_id"]
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        count = conn.execute(text("SELECT count(*) FROM artifacts WHERE project_id = :project_id"), {"project_id": project}).scalar_one()
    assert count == 1


def test_reimport_is_idempotent_under_concurrency_interruption_and_tamper(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    payload = _zip_with("data.bin", bytes(range(32)))
    notes = io.BytesIO()
    with zipfile.ZipFile(notes, "w") as archive:
        archive.writestr("data.bin", bytes(range(32)))
        archive.writestr("notes/complete.json", b'{"state":"complete"}')
    archive_bytes = notes.getvalue()
    first = store.import_archive(owner_id=owner, project_id=project, data=archive_bytes, media_type="application/zip")
    second = store.import_archive(owner_id=owner, project_id=project, data=archive_bytes, media_type="application/zip")
    assert first["artifact_id"] == second["artifact_id"]
    snapshot = store.root / "snapshots" / project / str(first["artifact_id"])
    data_path = snapshot / "data.bin"
    meta = store.root / "snapshot-meta" / project / f"{first['artifact_id']}.json"
    assert data_path.read_bytes() == bytes(range(32))
    assert meta.is_file()
    assert "snapshot-meta" not in snapshot.parts
    assert (snapshot / "notes" / "complete.json").read_bytes() == b'{"state":"complete"}'

    data_path.write_bytes(b"tampered")
    (snapshot / "complete").write_text('{"state":"complete"}', encoding="utf-8")
    meta.unlink()
    restored = store.import_archive(owner_id=owner, project_id=project, data=archive_bytes, media_type="application/zip")
    assert restored["artifact_id"] == first["artifact_id"]
    assert data_path.read_bytes() == bytes(range(32))
    assert not (snapshot / "complete").exists()
    assert meta.is_file()
    assert (snapshot / "notes" / "complete.json").read_bytes() == b'{"state":"complete"}'

    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def again() -> None:
        try:
            barrier.wait()
            results.append(store.import_archive(owner_id=owner, project_id=project, data=payload, media_type="application/zip"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=again), threading.Thread(target=again)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert results[0]["artifact_id"] == results[1]["artifact_id"]
    assert (store.root / "snapshots" / project / str(results[0]["artifact_id"]) / "data.bin").read_bytes() == bytes(range(32))


def test_import_rejects_traversal_symlinks_and_hardlinks(client: TestClient, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    with pytest.raises(ArtifactError) as escaped:
        store.import_archive(owner_id=owner, project_id=project, data=_zip_with("../escape.txt", b"pwn"), media_type="application/zip")
    assert escaped.value.code == "path_rejected"
    with pytest.raises(ArtifactError) as linked:
        store.import_archive(
            owner_id=owner,
            project_id=project,
            data=_zip_with("link", b"target", mode=stat.S_IFLNK | 0o777),
            media_type="application/zip",
        )
    assert linked.value.code == "symlink_rejected"
    hardlink = io.BytesIO()
    with tarfile.open(fileobj=hardlink, mode="w") as archive:
        member = tarfile.TarInfo("file.txt")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"data"))
        link = tarfile.TarInfo("hard.txt")
        link.type = tarfile.LNKTYPE
        link.linkname = "file.txt"
        archive.addfile(link)
    with pytest.raises(ArtifactError) as hard:
        store.import_archive(owner_id=owner, project_id=project, data=hardlink.getvalue(), media_type="application/x-tar")
    assert hard.value.code == "link_rejected"
    assert not any(path.name == "escape.txt" for path in store.root.rglob("*"))
