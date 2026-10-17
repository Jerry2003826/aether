"""Shared grading-file classification used by the sandbox restore and by patch publishing."""

from __future__ import annotations

from pathlib import Path

from aether_worker.grading_files import grading_paths, is_grading_path


def test_is_grading_path_covers_support_files_and_caches() -> None:
    assert not is_grading_path("app.py")
    for name in (
        "test_app.py",
        "oracle.py",
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
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


def test_grading_paths_lists_grading_files_and_skips_git(tmp_path: Path) -> None:
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


def test_grading_scope_is_an_explicit_denylist() -> None:
    from aether_worker import grading_files

    assert not hasattr(grading_files, "CANDIDATE_FILES")
    for name in ("app.py", "helper.py", "pkg/new_module.py", "pkg/__init__.py", "README.md", "src/lib/core.py"):
        assert not is_grading_path(name), name
    for name in (
        "test_app.py",
        "pkg/test_core.py",
        "core_test.py",
        "tests/helpers.py",
        "pkg/tests/data.json",
        "oracle.py",
        "pkg/oracle.py",
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "sitecustomize.py",
        "usercustomize.py",
        "evil.pth",
        "__pycache__/helper.cpython-312.pyc",
        ".pytest_cache/v/cache/lastfailed",
    ):
        assert is_grading_path(name), name


def test_startup_hooks_and_job_editable_override() -> None:
    from aether_worker.grading_files import is_startup_hook

    for name in ("sitecustomize.py", "usercustomize.py", "x.pth", "pkg/evil.pth"):
        assert is_startup_hook(name), name
    for name in ("helper.py", "conftest.py", "test_app.py"):
        assert not is_startup_hook(name), name
    # A job-level editable list wins over the denylist, except for interpreter startup hooks.
    assert not is_grading_path("pyproject.toml", editable={"pyproject.toml"})
    assert is_grading_path("pyproject.toml", editable={"other.toml"})
    assert is_grading_path("sitecustomize.py", editable={"sitecustomize.py"})
    assert is_grading_path("__pycache__/a.pyc", editable={"__pycache__/a.pyc"})


def test_grading_paths_uses_the_denylist(tmp_path: Path) -> None:
    for name in ("app.py", "helper.py", "oracle.py", "tests/test_x.py", "__pycache__/a.pyc", "setup.cfg"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    assert grading_paths(tmp_path) == {"oracle.py", "tests/test_x.py", "__pycache__/a.pyc", "setup.cfg"}
    assert grading_paths(tmp_path, editable={"setup.cfg"}) == {"oracle.py", "tests/test_x.py", "__pycache__/a.pyc"}


def test_hidden_pytest_configs_are_grading_files() -> None:
    for name in (".pytest.ini", "pytest.toml", ".pytest.toml", "pkg/.pytest.ini"):
        assert is_grading_path(name), name


def test_grading_protocol_never_reads_pytest_configuration() -> None:
    """Grading runs the stdlib protocol on root test_*.py, so no pytest config (baseline or added) applies."""
    import ast

    source = Path("services/worker/aether_worker/coding_protocol.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {"pytest", "_pytest", "pluggy", "tomllib", "configparser"}
    assert 'glob("test_*.py")' in source
