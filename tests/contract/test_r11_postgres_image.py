"""The container the contract tests run against is the PostgreSQL image pinned in infra/images.lock.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_container_runs_the_locked_pgvector_image(running_postgres_image) -> None:
    lock = json.loads((ROOT / "infra" / "images.lock.json").read_text(encoding="utf-8"))
    pinned = next(item for item in lock["images"] if item["repository"] == "pgvector/pgvector")
    # The fixture reads RepoDigests from docker inspect of the image behind the running container; the
    # string handed to PostgresContainer came from this same lock, so comparing it would prove nothing.
    assert f"{pinned['repository']}@{pinned['digest']}" in running_postgres_image.repo_digests, running_postgres_image.repo_digests
