"""Round-2 regressions on real PostgreSQL. These are positive checks, not inverted skips."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import uuid
import zipfile

import pytest
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.db import Database
from aether_worker.goal_effect import WorkerEndpoints, _ensure_verification
from tests.integration.test_artifacts import _project


def _store(client, api_db) -> ArtifactStore:
    return ArtifactStore(client.app.state.settings.artifact_root, api_db)


def _endpoints(role_urls, root) -> WorkerEndpoints:
    return WorkerEndpoints(
        worker_url=role_urls["aether_worker"],
        verifier_url=role_urls["aether_verifier"],
        dispatcher_url=role_urls["aether_dispatcher"],
        artifact_root=root,
        temporal_address="127.0.0.1:9",
        task_queue="audit-round2",
    )


def _contract(worker: Database, owner: str, project: str, task_id: str) -> tuple[str, str]:
    content = '{"criterion_id":"goal_artifact"}'
    digest = sha256_text(content)
    with worker.transaction(owner_id=owner, project_id=project) as conn:
        contract_id = str(
            conn.execute(
                text(
                    "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                    "VALUES (:project_id, :task_id, 1, CAST(:content AS jsonb), :digest, :owner, now()) RETURNING id::text"
                ),
                {
                    "project_id": project,
                    "task_id": task_id,
                    "content": content,
                    "digest": digest,
                    "owner": owner,
                },
            ).scalar_one()
        )
    return contract_id, digest


def _task(worker: Database, project: str, goal: str) -> str:
    with worker.transaction(project_id=project) as conn:
        return str(
            conn.execute(
                text(
                    "INSERT INTO tasks (project_id, original_goal, workflow_id, status) "
                    "VALUES (:project_id, :goal, :workflow, 'RUNNING') RETURNING id::text"
                ),
                {"project_id": project, "goal": goal, "workflow": f"round2-{uuid.uuid4()}"},
            ).scalar_one()
        )


def test_database_fetch_methods_read_postgres_rows(api_db) -> None:
    with api_db.transaction() as conn:
        one = api_db.fetch_one(conn, "SELECT 42 AS value")
        many = api_db.fetch_all(conn, "SELECT generate_series(1, 2) AS value")
    assert one == {"value": 42}
    assert many == [{"value": 1}, {"value": 2}]


def test_journal_short_write_is_not_left_truncated(client, api_db, monkeypatch) -> None:
    _owner, project, _headers = _project(client)
    store = _store(client, api_db)
    original = os.write

    def short_write(fd, data):
        payload = bytes(data)
        return original(fd, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(os, "write", short_write)
    record = {"attempt_id": "short-write-check", "state": "prepared"}
    store._journal(project, record)
    assert store._journal_rows(project) == [record]


def test_truncated_journal_tail_does_not_block_valid_recovery(client, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    staged = store.stage_without_commit(project_id=project, data=b"known-good", media_type="text/plain")
    path = store.root / "journal" / f"{project}.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"state":"resolved","attempt_id":')
        handle.flush()
        os.fsync(handle.fileno())
    restored = store.recover(owner_id=owner, project_id=project)
    assert staged["artifact_id"] in restored
    data, media = store.get(owner_id=owner, project_id=project, artifact_id=staged["artifact_id"])
    assert (data, media) == (b"known-good", "text/plain")
    quarantined = list((store.root / "journal-quarantine").rglob("*"))
    assert any(path.is_file() and b"attempt_id" in path.read_bytes() for path in quarantined)
    again = store.recover(owner_id=owner, project_id=project)
    assert again == []


def test_corrupt_middle_journal_line_is_reported(client, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    staged = store.stage_without_commit(project_id=project, data=b"before-gap", media_type="text/plain")
    path = store.root / "journal" / f"{project}.jsonl"
    with path.open("ab") as handle:
        handle.write(b"{not-json}\n")
        handle.flush()
        os.fsync(handle.fileno())
    with pytest.raises(ArtifactError) as corrupt:
        store.recover(owner_id=owner, project_id=project)
    assert corrupt.value.code == "journal_corrupt"
    with pytest.raises(ArtifactError):
        store.get(owner_id=owner, project_id=project, artifact_id=staged["artifact_id"])


@pytest.mark.parametrize("media", ["application/zip", "application/x-tar"])
def test_dot_slash_archive_members_publish(client, api_db, media) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    if media == "application/zip":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("./src/main.py", b"print(42)\n")
        data = buffer.getvalue()
    else:
        buffer = io.BytesIO()
        payload = b"print(42)\n"
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("./src/main.py")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        data = buffer.getvalue()
    stored = store.import_archive(owner_id=owner, project_id=project, data=data, media_type=media)
    assert (store.root / "snapshots" / project / str(stored["artifact_id"]) / "src" / "main.py").read_bytes() == b"print(42)\n"


def test_canonical_archive_collision_is_rejected(client, api_db) -> None:
    owner, project, _headers = _project(client)
    store = _store(client, api_db)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("./src/main.py", b"one")
        archive.writestr("src/main.py", b"two")
    with pytest.raises(ArtifactError) as collision:
        store.import_archive(owner_id=owner, project_id=project, data=buffer.getvalue(), media_type="application/zip")
    assert collision.value.code == "archive_collision"


def test_goal_verification_reads_bytes_and_does_not_reuse_another_candidate(client, api_db, role_urls) -> None:
    owner, project, headers = _project(client)
    store = _store(client, api_db)
    worker = Database(role_urls["aether_worker"])
    source = store.put(owner_id=owner, project_id=project, data="事实：水在标准大气压下于一百摄氏度沸腾\n".encode(), media_type="text/plain")
    goal = "请根据已上传资料回答沸点"
    task_id = _task(worker, project, goal)
    answer = store.put(
        owner_id=owner,
        project_id=project,
        data="资料记载：事实：水在标准大气压下于一百摄氏度沸腾".encode(),
        media_type="text/plain",
        classification="task_answer",
    )
    with worker.transaction(owner_id=owner, project_id=project) as conn:
        conn.execute(
            text("UPDATE artifacts SET task_id = :task_id WHERE project_id = :project_id AND id = :id"),
            {"task_id": task_id, "project_id": project, "id": answer["artifact_id"]},
        )
    contract_id, digest = _contract(worker, owner, project, task_id)
    endpoints = _endpoints(role_urls, store.root)
    first = _ensure_verification(
        endpoints,
        owner_id=owner,
        project_id=project,
        task_id=task_id,
        contract_id=contract_id,
        contract_sha=digest,
        artifact_id=str(answer["artifact_id"]),
        goal=goal,
    )
    with Database(role_urls["aether_verifier"]).transaction(owner_id=owner, project_id=project) as conn:
        row = conn.execute(
            text("SELECT verdict, artifact_id::text AS artifact_id, artifact_sha256 FROM verification_results WHERE id = :id"),
            {"id": first},
        ).mappings().one()
    assert row["verdict"] == "PASS"
    assert row["artifact_id"] == answer["artifact_id"]
    assert row["artifact_sha256"] == hashlib.sha256("资料记载：事实：水在标准大气压下于一百摄氏度沸腾".encode()).hexdigest()

    path = store.root / project / str(answer["sha256"])[:2] / str(answer["sha256"])
    path.write_bytes(b"corrupt document!")
    fresh_task = _task(worker, project, goal)
    fresh_contract, fresh_digest = _contract(worker, owner, project, fresh_task)
    corrupt_answer = str(answer["artifact_id"])
    # The corrupt bytes belong to the first answer. A second candidate is stored intact, then we point
    # the corrupt check at the damaged id on its own task so the row cannot be the first PASS.
    damaged = store.put(owner_id=owner, project_id=project, data=b"intact-then-damaged", media_type="text/plain", classification="task_answer")
    damaged_path = store.root / project / str(damaged["sha256"])[:2] / str(damaged["sha256"])
    damaged_path.write_bytes(b"not-the-stored-bytes")
    with worker.transaction(owner_id=owner, project_id=project) as conn:
        conn.execute(
            text("UPDATE artifacts SET task_id = :task_id WHERE project_id = :project_id AND id = :id"),
            {"task_id": fresh_task, "project_id": project, "id": damaged["artifact_id"]},
        )
    failed = _ensure_verification(
        endpoints,
        owner_id=owner,
        project_id=project,
        task_id=fresh_task,
        contract_id=fresh_contract,
        contract_sha=fresh_digest,
        artifact_id=str(damaged["artifact_id"]),
        goal=goal,
    )
    with Database(role_urls["aether_verifier"]).transaction(owner_id=owner, project_id=project) as conn:
        failed_row = conn.execute(
            text("SELECT verdict, artifact_id::text AS artifact_id FROM verification_results WHERE id = :id"),
            {"id": failed},
        ).mappings().one()
    assert failed_row["verdict"] != "PASS"
    assert failed_row["artifact_id"] == damaged["artifact_id"]
    assert failed != first

    tombstone_task = _task(worker, project, goal)
    tombstone_contract, tombstone_digest = _contract(worker, owner, project, tombstone_task)
    hidden = store.put(owner_id=owner, project_id=project, data="资料记载：事实：水在标准大气压下于一百摄氏度沸腾。".encode(), media_type="text/plain", classification="task_answer")
    with worker.transaction(owner_id=owner, project_id=project) as conn:
        conn.execute(
            text("UPDATE artifacts SET task_id = :task_id WHERE project_id = :project_id AND id = :id"),
            {"task_id": tombstone_task, "project_id": project, "id": hidden["artifact_id"]},
        )
    store.tombstone(owner_id=owner, project_id=project, artifact_id=str(hidden["artifact_id"]))
    hidden_verdict = _ensure_verification(
        endpoints,
        owner_id=owner,
        project_id=project,
        task_id=tombstone_task,
        contract_id=tombstone_contract,
        contract_sha=tombstone_digest,
        artifact_id=str(hidden["artifact_id"]),
        goal=goal,
    )
    with Database(role_urls["aether_verifier"]).transaction(owner_id=owner, project_id=project) as conn:
        hidden_row = conn.execute(text("SELECT verdict FROM verification_results WHERE id = :id"), {"id": hidden_verdict}).mappings().one()
    assert hidden_row["verdict"] != "PASS"

    other = store.put(
        owner_id=owner,
        project_id=project,
        data="资料记载：事实：冰点在标准大气压下为零摄氏度".encode(),
        media_type="text/plain",
        classification="task_answer",
    )
    # Source for the second candidate is the original source plus nothing about 冰点 in the first source.
    # Put a second source line by uploading another unbound document before verification.
    store.put(owner_id=owner, project_id=project, data="事实：冰点在标准大气压下为零摄氏度\n".encode(), media_type="text/plain")
    other_task = _task(worker, project, "请回答冰点")
    with worker.transaction(owner_id=owner, project_id=project) as conn:
        conn.execute(
            text("UPDATE artifacts SET task_id = :task_id WHERE project_id = :project_id AND id = :id"),
            {"task_id": other_task, "project_id": project, "id": other["artifact_id"]},
        )
    other_contract, other_digest = _contract(worker, owner, project, other_task)
    second = _ensure_verification(
        endpoints,
        owner_id=owner,
        project_id=project,
        task_id=other_task,
        contract_id=other_contract,
        contract_sha=other_digest,
        artifact_id=str(other["artifact_id"]),
        goal="请回答冰点",
    )
    assert second != first
    with Database(role_urls["aether_verifier"]).transaction(owner_id=owner, project_id=project) as conn:
        second_row = conn.execute(
            text("SELECT artifact_id::text AS artifact_id, contract_id::text AS contract_id, verdict FROM verification_results WHERE id = :id"),
            {"id": second},
        ).mappings().one()
    assert second_row["artifact_id"] == other["artifact_id"]
    assert second_row["contract_id"] == other_contract
    assert second_row["verdict"] == "PASS"
