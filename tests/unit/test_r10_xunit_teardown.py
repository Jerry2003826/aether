"""pytest-style teardowns run even when the matching setup is not defined (pytest semantics)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()


def _verdicts(root: Path, test_module: str) -> dict[str, tuple[str, str]]:
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "test_hooks.py").write_text(test_module, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=root,
        # The protocol grades only with its shared-memory directory named (the entry names the emptied one).
        env={**os.environ, "AETHER_PROTOCOL_SHM_DIR": "/dev/shm"},
        capture_output=True,
        text=True,
        timeout=60,
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


# Each teardown raises its own exception type, so a FAIL with that name proves the hook ran.
@pytest.mark.parametrize(
    ("module", "name", "error"),
    [
        (
            (
                "import app\n\nclass TestOnlyTeardownMethod:\n"
                "    def teardown_method(self, method):\n        raise LookupError(method.__name__)\n\n"
                "    def test_value(self):\n        assert app.value() == 1\n"
            ),
            "test_hooks.TestOnlyTeardownMethod.test_value",
            "LookupError",
        ),
        (
            (
                "import app\n\nclass TestOnlyTeardownClass:\n"
                "    @classmethod\n    def teardown_class(cls):\n        raise UnicodeError('class')\n\n"
                "    def test_value(self):\n        assert app.value() == 1\n"
            ),
            "test_hooks.TestOnlyTeardownClass.test_value",
            "UnicodeError",
        ),
        (
            (
                "import app\n\nclass TestTeardownMethodWithSetupClassOnly:\n"
                "    @classmethod\n    def setup_class(cls):\n        cls.ready = True\n\n"
                "    def teardown_method(self, method):\n        raise ZeroDivisionError(method.__name__)\n\n"
                "    def test_value(self):\n        assert self.ready and app.value() == 1\n"
            ),
            "test_hooks.TestTeardownMethodWithSetupClassOnly.test_value",
            "ZeroDivisionError",
        ),
        (
            (
                "import app\n\ndef teardown_function(function):\n    raise BufferError(function.__name__)\n\n"
                "def test_value():\n    assert app.value() == 1\n"
            ),
            "test_hooks.test_value",
            "BufferError",
        ),
        (
            (
                "import app\n\ndef teardown_module(module):\n    raise EOFError(module.__name__)\n\n"
                "def test_value():\n    assert app.value() == 1\n"
            ),
            "test_hooks.test_value",
            "EOFError",
        ),
        (
            (
                "import app\n\ndef teardown_module(module):\n    raise EOFError(module.__name__)\n\n"
                "class TestPlain:\n    def test_value(self):\n        assert app.value() == 1\n"
            ),
            "test_hooks.TestPlain.test_value",
            "EOFError",
        ),
    ],
    ids=[
        "teardown_method",
        "teardown_class",
        "teardown_method_with_setup_class",
        "teardown_function",
        "teardown_module",
        "teardown_module_class",
    ],
)
def test_a_teardown_without_its_setup_still_runs(
    tmp_path: Path, module: str, name: str, error: str
) -> None:
    assert _verdicts(tmp_path, module) == {name: ("FAIL", error)}


def test_a_failing_setup_method_still_skips_its_teardown(tmp_path: Path) -> None:
    # pytest: when setup_method raises, teardown_method does not run; the setup error is the verdict.
    module = (
        "import app\n\nclass TestBrokenSetup:\n"
        "    def setup_method(self, method):\n        raise KeyError('setup')\n\n"
        "    def teardown_method(self, method):\n        raise LookupError('teardown')\n\n"
        "    def test_value(self):\n        assert app.value() == 1\n"
    )
    assert _verdicts(tmp_path, module) == {
        "test_hooks.TestBrokenSetup.test_value": ("FAIL", "KeyError")
    }
