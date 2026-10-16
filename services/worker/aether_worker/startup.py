"""Startup reconciliation for artifact journals."""

from __future__ import annotations

from pathlib import Path

from aether_storage.artifacts import ArtifactStore
from aether_worker.project_registry import load_projects


def recover_started_projects(store: ArtifactStore, index_root: Path) -> list[str]:
    """Insert prepared journal rows for projects this worker has already seen."""
    restored: list[str] = []
    for project_id, owner_id in load_projects(index_root).items():
        if not owner_id:
            continue
        restored.extend(store.recover(owner_id=owner_id, project_id=project_id))
    return restored
