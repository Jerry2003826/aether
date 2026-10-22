"""A stored embedding reads back from PostgreSQL as the exact float32 values that were written."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.retention import commit_embedding, current_epoch
from aether_worker.retrieval import DIMENSION, EMBEDDING_REVISION, _vector_literal
from sqlalchemy import text

from tests.integration.test_hybrid_retrieval import (
    _chunk_ids,
    _owner_and_project,
    _store,
)


def test_vector_param_roundtrips_exact_float32(client, role_urls, tmp_path: Path) -> None:
    owner_id, project_id = _owner_and_project(client, "r11-pgvector", "向量")
    database = Database(role_urls["aether_worker"])
    store = ArtifactStore(tmp_path / "artifacts", database)
    _store(database, store, owner_id, project_id, "vec", "向量往返精度检查")
    chunk_id = next(iter(_chunk_ids(database, owner_id, project_id).values()))
    rng = np.random.default_rng(11)
    vector = (rng.standard_normal(DIMENSION) * 1e-6).astype(np.float32)
    vector[0] = np.float32(1e-9)
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        assert commit_embedding(
            conn,
            project_id=project_id,
            chunk_id=chunk_id,
            embedding=_vector_literal([float(value) for value in vector]),
            revision=EMBEDDING_REVISION,
            seen_epoch=current_epoch(conn, project_id),
        )
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        stored = conn.execute(
            text("SELECT embedding::text FROM chunks WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": chunk_id},
        ).scalar_one()
    back = np.asarray([float(part) for part in stored[1:-1].split(",")], dtype=np.float32)
    assert back.shape == (DIMENSION,)
    assert np.array_equal(back, vector)
