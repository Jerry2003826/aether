"""A module candidate code writes into the workspace while being graded is never imported by a test."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()

# Candidate code: at call time it writes three new modules, each of which marks the process it runs in.
_APP = (
    "import os\n\n"
    "BODY = 'import os\\nos.environ[\"INJECTED_RAN\"] = __name__\\nVALUE = 42\\n'\n\n"
    "def plant():\n"
    "    for name in ('injected.py', os.path.join('tests', 'injected_local.py'), os.path.join('pkg', 'evil.py')):\n"
    "        with open(name, 'w') as handle:\n            handle.write(BODY)\n"
    "    return 1\n"
)
_CHECK = (
    "import importlib\nimport os\nimport sys\n\n"
    "def attempt(name):\n"
    "    importlib.invalidate_caches()\n"
    "    try:\n        importlib.import_module(name)\n    except ImportError:\n        imported = False\n"
    "    else:\n        imported = True\n"
    "    assert 'INJECTED_RAN' not in os.environ, os.environ['INJECTED_RAN']\n"
    "    assert not imported, name\n"
)
_FILES = {
    "app.py": _APP,
    "pkg/__init__.py": "",
    "tests/check.py": _CHECK,
    # Plants at runtime, then imports in the same test process.
    "tests/test_a_plant.py": (
        "import app\nfrom check import attempt\n\n"
        "def test_plant_then_import_root():\n    assert app.plant() == 1\n    attempt('injected')\n\n"
        "def test_plant_then_import_local():\n    assert app.plant() == 1\n    attempt('injected_local')\n\n"
        "def test_plant_then_import_package_member():\n    assert app.plant() == 1\n    attempt('pkg.evil')\n"
    ),
    # A later test process: the planted files already exist on disk when it starts.
    "tests/test_b_later.py": (
        "from check import attempt\n\n"
        "def test_later_root():\n    attempt('injected')\n\n"
        "def test_later_local():\n    attempt('injected_local')\n\n"
        "def test_later_package_member():\n    attempt('pkg.evil')\n"
    ),
}


def _grade(root: Path, baseline: Path | None) -> dict[str, tuple[str, str]]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("AETHER_")
    }
    if baseline is not None:
        env["AETHER_PROTOCOL_BASELINE_DIR"] = str(baseline)
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    body = json.loads(
        next(
            line
            for line in reversed(completed.stdout.splitlines())
            if line.startswith("{")
        )
    )
    return {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}


@pytest.mark.parametrize(
    "with_baseline", [False, True], ids=["snapshot", "baseline_dir"]
)
def test_modules_created_after_the_snapshot_are_not_importable_by_tests(
    tmp_path: Path, with_baseline: bool
) -> None:
    root = tmp_path / "job"
    for name, text in _FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    baseline = None
    if with_baseline:
        baseline = tmp_path / "work"
        shutil.copytree(root, baseline)
    verdicts = _grade(root, baseline)
    assert verdicts == {
        f"tests.test_a_plant.{name}": ("PASS", "")
        for name in (
            "test_plant_then_import_local",
            "test_plant_then_import_package_member",
            "test_plant_then_import_root",
        )
    } | {
        f"tests.test_b_later.{name}": ("PASS", "")
        for name in ("test_later_local", "test_later_package_member", "test_later_root")
    }
    assert (root / "injected.py").is_file()  # the candidate really did plant them
