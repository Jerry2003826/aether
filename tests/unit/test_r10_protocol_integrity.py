"""Candidate code that runs during grading cannot change what the remaining grading files say."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()


def _grade(root: Path) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    line = next(item for item in reversed(completed.stdout.splitlines()) if item.startswith("{"))
    return completed.returncode, json.loads(line)


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_candidate_rewriting_a_later_test_file_cannot_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            # Imported by the first test module; rewrites the second before it is loaded.
            "app.py": (
                "import pathlib\n"
                "pathlib.Path(__file__).with_name('test_b.py').write_text('def test_b():\\n    pass\\n')\n"
                "def value():\n    return 0\n"
            ),
            "test_a.py": "import app\n\ndef test_a():\n    assert True\n",
            "test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert code == 1, body
    assert body["passed"] < 2
    assert body["failed"] >= 1
    assert not any(case["name"] == "test_b.test_b" and case["verdict"] == "PASS" for case in body["cases"]), body


def test_candidate_rewriting_a_support_module_cannot_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import pathlib\n"
                "pathlib.Path(__file__).with_name('oracle.py').write_text('def expected():\\n    return 0\\n')\n"
                "def value():\n    return 0\n"
            ),
            "oracle.py": "def expected():\n    return 42\n",
            "test_a.py": "import app\n\ndef test_a():\n    assert True\n",
            "test_b.py": "import app\nimport oracle\n\ndef test_b():\n    assert app.value() == oracle.expected()\n",
        },
    )
    code, body = _grade(tmp_path)
    assert code == 1, body
    assert body["failed"] >= 1
    assert not any(case["name"] == "test_b.test_b" and case["verdict"] == "PASS" for case in body["cases"]), body


def test_tampering_during_the_run_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "import pathlib\n"
                "pathlib.Path(__file__).with_name('test_b.py').write_text('def test_b():\\n    pass\\n')\n"
                "def value():\n    return 42\n"
            ),
            "test_a.py": "import app\n\ndef test_a():\n    assert app.value() == 42\n",
            "test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n",
        },
    )
    code, body = _grade(tmp_path)
    assert code == 1, body
    assert {"name": "suite", "verdict": "FAIL", "detail": "grading_tampered"} in body["cases"]


def test_an_honest_suite_still_passes_and_packages_import(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg.core import answer\n",
            "pkg/core.py": "def answer():\n    return 42\n",
            "app.py": "from pkg import answer\n\ndef value():\n    return answer()\n",
            "oracle.py": "def expected():\n    return 42\n",
            "test_a.py": "import app\nimport oracle\n\ndef test_a():\n    assert app.value() == oracle.expected()\n",
            "test_b.py": "import app\n\ndef test_b():\n    assert app.value() == 42\n\nasync def test_c():\n    pass\n",
        },
    )
    code, body = _grade(tmp_path)
    assert body["passed"] == 2, body
    assert {"name": "test_b.test_c", "verdict": "FAIL", "detail": "async"} in body["cases"]
    assert code == 1
    (tmp_path / "test_b.py").write_text("import app\n\ndef test_b():\n    assert app.value() == 42\n", encoding="utf-8")
    code, body = _grade(tmp_path)
    assert (code, body["passed"], body["failed"]) == (0, 2, 0), body
