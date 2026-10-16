"""Projects a worker has already seen. Restart reads this instead of scanning every tenant."""

from __future__ import annotations

import json
from pathlib import Path


def load_projects(index_root: Path) -> dict[str, str]:
    path = index_root / "projects.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("projects.json 必须是项目到所有者的映射")
    projects: dict[str, str] = {}
    for project_id, owner_id in loaded.items():
        if not isinstance(project_id, str) or not isinstance(owner_id, str) or not owner_id:
            raise ValueError("projects.json 里的项目映射不合法")
        projects[project_id] = owner_id
    return projects


def save_projects(index_root: Path, projects: dict[str, str]) -> None:
    index_root.mkdir(parents=True, exist_ok=True)
    path = index_root / "projects.json"
    temporary = path.with_name("projects.json.tmp")
    temporary.write_text(json.dumps(projects, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
