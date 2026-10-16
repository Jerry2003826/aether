"""S-041 and S-042: startup reconciles prepared bytes, and get survives a short read."""

from __future__ import annotations

import os
from pathlib import Path

from aether_storage.artifacts import ArtifactStore
from aether_worker.project_registry import save_projects
from aether_worker.startup import recover_started_projects


def test_get_survives_a_short_read(client, api_db, monkeypatch) -> None:
    owner_id, project_id, _headers = _project(client)
    payload = b"short-read-payload-that-must-come-back-whole"
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    stored = store.put(owner_id=owner_id, project_id=project_id, data=payload, media_type="text/plain")
    real_read = os.read

    def short(fd: int, size: int) -> bytes:
        return real_read(fd, 1)

    monkeypatch.setattr(os, "read", short)
    data, media_type = store.get(owner_id=owner_id, project_id=project_id, artifact_id=stored["artifact_id"])
    assert data == payload
    assert media_type == "text/plain"


def test_startup_recover_inserts_a_prepared_journal(client, role_urls, tmp_path: Path) -> None:
    from aether_storage.db import Database

    owner_id, project_id, _headers = _project(client)
    root = client.app.state.settings.artifact_root
    store = ArtifactStore(root, Database(role_urls["aether_worker"]))
    staged = store.stage_without_commit(project_id=project_id, data=b"prepared-before-start", media_type="text/plain")
    index_root = tmp_path / "indexes"
    save_projects(index_root, {project_id: owner_id})
    restored = recover_started_projects(store, index_root)
    assert staged["artifact_id"] in restored
    data, media_type = store.get(owner_id=owner_id, project_id=project_id, artifact_id=staged["artifact_id"])
    assert data == b"prepared-before-start"
    assert media_type == "text/plain"
    assert recover_started_projects(store, index_root) == []


def _project(client):
    from tests.integration.test_artifacts import _project as project

    return project(client)
