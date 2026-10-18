"""The sandbox image tag covers .dockerignore (Codex openhands_image.py:98).

.dockerignore decides which files of the build context reach `docker build`, so changing it can change
the built image; a tag that ignores it would reuse an image built from a different context.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from aether_worker import openhands_image


def _copy_context(target: Path) -> None:
    root = openhands_image.repository_root()
    for path in openhands_image._context_files(root):
        relative = path.relative_to(root)
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target / relative)
    shutil.copyfile(root / ".dockerignore", target / ".dockerignore")


def test_changing_dockerignore_changes_the_sandbox_image_tag(tmp_path: Path) -> None:
    _copy_context(tmp_path)
    before = openhands_image.sandbox_tag(tmp_path)
    ignore = tmp_path / ".dockerignore"
    ignore.write_text(ignore.read_text(encoding="utf-8") + "environments/openhands/wheels\n", encoding="utf-8")
    assert openhands_image.sandbox_tag(tmp_path) != before


def test_the_tag_is_stable_when_nothing_changes(tmp_path: Path) -> None:
    _copy_context(tmp_path)
    assert openhands_image.sandbox_tag(tmp_path) == openhands_image.sandbox_tag(tmp_path)
