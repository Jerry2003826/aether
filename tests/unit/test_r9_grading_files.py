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


_FORCE_PASS = (
    "import pytest\n\n"
    "@pytest.hookimpl(wrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    report = yield\n"
    "    report.outcome = 'passed'\n"
    "    return report\n\n"
    "def pytest_collection_modifyitems(items):\n"
    "    items[:] = [item for item in items if 'fail' not in item.name]\n"
)


def test_grading_never_reads_candidate_pytest_configuration_or_plugins(tmp_path: Path) -> None:
    """Grading runs pytest (ADR-F44), but nothing in the workspace or the environment configures it:
    no config file, conftest, plugin or PYTEST_* variable can hide a failing test or make it pass."""
    import json
    import os
    import subprocess
    import sys

    files = {
        "app.py": "def value():\n    return 1\n",
        "test_mod.py": "import app\n\ndef test_ok():\n    assert app.value() == 1\n\ndef test_fail():\n    assert app.value() == 2\n",
        "pytest.ini": "[pytest]\npython_functions = nothing\naddopts = --deselect test_mod.py::test_fail -p forcepass\n",
        "pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["nothing.py"]\naddopts = "-k test_ok"\n',
        "setup.cfg": "[tool:pytest]\naddopts = -k test_ok\n",
        "tox.ini": "[pytest]\naddopts = -k test_ok\n",
        "conftest.py": _FORCE_PASS,
        "sub/conftest.py": _FORCE_PASS,
        "forcepass.py": _FORCE_PASS,
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AETHER_", "PYTEST_"))}
    env.update(
        {
            "AETHER_PROTOCOL_SHM_DIR": "/dev/shm",
            "PYTEST_ADDOPTS": "--deselect test_mod.py::test_fail -p forcepass -k test_ok",
            "PYTEST_PLUGINS": "forcepass",
            "PYTHONPATH": str(tmp_path),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-I", str(Path("services/worker/aether_worker/coding_protocol.py").resolve())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    body = json.loads(completed.stdout.strip().splitlines()[-1])
    verdicts = {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}
    assert verdicts == {"test_mod.test_ok": ("PASS", ""), "test_mod.test_fail": ("FAIL", "AssertionError")}, completed.stdout + completed.stderr
    assert completed.returncode == 1


def test_new_top_level_modules_that_shadow_imports_are_detected() -> None:
    from aether_worker.grading_files import is_shadow_module
    from aether_worker.verification import _SHADOW_ROOTS

    for name in sorted(_SHADOW_ROOTS):
        assert is_shadow_module(name) or is_shadow_module(name + "/__init__.py"), name
    for name in ("pytest.py", "_pytest/__init__.py", "pluggy.py", "json.py", "json/__init__.py", "os.py", "pluggy/__init__.py"):
        assert is_shadow_module(name), name
    for name in ("helper.py", "newmod.py", "app.py", "pkg/json.py", "README.md"):
        assert not is_shadow_module(name), name


def test_shadow_set_comes_from_the_installed_names_source(monkeypatch) -> None:
    """Modelled on a minimal sandbox image: only its own packages, stdlib/builtins and SHADOW_ROOTS shadow."""
    from aether_worker import grading_files

    monkeypatch.setattr(grading_files, "_installed_top_level", lambda: frozenset({"openhands", "litellm"}))
    grading_files.shadow_names.cache_clear()
    try:
        for name in ("utils.py", "config.py", "utils/__init__.py", "helper.py"):
            assert not grading_files.is_shadow_module(name), name
        for name in ("json.py", "os.py", "litellm.py", "openhands/__init__.py", "pytest.py", "_pytest/__init__.py"):
            assert grading_files.is_shadow_module(name), name
        monkeypatch.setattr(grading_files, "_installed_top_level", lambda: frozenset({"utils"}))
        grading_files.shadow_names.cache_clear()
        assert grading_files.is_shadow_module("utils.py")
    finally:
        grading_files.shadow_names.cache_clear()


def test_worker_side_never_computes_the_shadow_set() -> None:
    """Only sandbox-side code (the entry and the protocol it runs) may call the shadow functions; the worker
    consumes grading_shadow_module only."""
    import ast

    allowed = {"grading_files.py", "openhands_sandbox_entry.py", "coding_protocol.py"}
    for path in sorted(Path("services").rglob("*.py")):
        if path.name in allowed or ".venv" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
        assert not names & {"is_shadow_module", "shadow_names", "_installed_top_level"}, path
