"""Shared grading-file classification used by the sandbox restore and by patch publishing."""

from __future__ import annotations

from pathlib import Path

from aether_worker.grading_files import CANDIDATE_FILES, grading_paths, is_grading_path


def test_is_grading_path_covers_support_files_and_caches() -> None:
    assert "app.py" in CANDIDATE_FILES
    assert not is_grading_path("app.py")
    for name in (
        "test_app.py",
        "oracle.py",
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "__init__.py",
        "evil.pth",
        "pkg/conftest.py",
        "__pycache__/app.cpython-312.pyc",
        ".pytest_cache/v/cache/lastfailed",
        "pkg/__pycache__/x.pyc",
        "build/lib/app.py",
        "demo.egg-info/PKG-INFO",
        ".aether-state/base_state.json",
    ):
        assert is_grading_path(name), name
    assert is_grading_path("./oracle.py")
    assert not is_grading_path("./app.py")


def test_grading_paths_lists_every_non_candidate_file(tmp_path: Path) -> None:
    for name in ("app.py", "oracle.py", "test_app.py", "sub/conftest.py", ".pytest_cache/x", "__pycache__/a.pyc"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    assert grading_paths(tmp_path) == {
        "oracle.py",
        "test_app.py",
        "sub/conftest.py",
        ".pytest_cache/x",
        "__pycache__/a.pyc",
    }
