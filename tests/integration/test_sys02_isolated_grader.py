"""SYS-02: protocol assertions stay outside the candidate; whitebox pytest cannot certify."""

from __future__ import annotations

from pathlib import Path

import pytest

from aether_worker.runner import docker_available
from aether_worker.verification import export_candidate, grade, text_sha256

PROTOCOL = "import candidate\n\ndef test_required_result():\n    assert candidate.value() == 42\n"
WHITEBOX = "import candidate\n\ndef test_required_result():\n    seen = candidate.value()\n    assert seen == 42\n"
CONTRACT = "合同：value() 必须是 42"

CORRECT = "def value():\n    return 42\n"
WRONG = "def value():\n    return 0\n"
MONKEYPATCH = (
    "import _pytest.python\n"
    "_pytest.python.Function.runtest = lambda self: None\n"
    "def value():\n"
    "    return 0\n"
)
FD_ATTACK = (
    "import __main__, json, os\n"
    "os.write(__main__.fd, json.dumps({'exit_code': 0, 'passed_count': 1}).encode())\n"
    "os._exit(0)\n"
    "def value():\n"
    "    return 0\n"
)


def _grade(tmp_path: Path, source: str, suite_text: str, name: str = "case") -> dict:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    root = tmp_path / name
    candidate = root / "candidate"
    suite = root / "suite"
    candidate.mkdir(parents=True)
    suite.mkdir()
    (candidate / "candidate.py").write_text(source, encoding="utf-8")
    (suite / "test_trusted.py").write_text(suite_text, encoding="utf-8")
    return grade(
        candidate,
        suite,
        suite_sha256=export_candidate(suite)["candidate_sha256"],
        contract_text=CONTRACT,
        contract_sha256=text_sha256(CONTRACT),
        candidate_sha256=export_candidate(candidate)["candidate_sha256"],
    )


def test_protocol_correct_implementation_passes(tmp_path: Path) -> None:
    graded = _grade(tmp_path, CORRECT, PROTOCOL)
    assert graded["verdict"] == "PASS", graded["reason"]
    assert graded["trust"] == "isolated"
    assert graded["security_certified"] is True
    assert graded["auto_promote"] is False


def test_protocol_wrong_implementation_fails(tmp_path: Path) -> None:
    graded = _grade(tmp_path, WRONG, PROTOCOL)
    assert graded["verdict"] == "FAIL", graded["reason"]
    assert graded["security_certified"] is not True
    assert graded["auto_promote"] is not True


def test_protocol_pytest_monkeypatch_cannot_pass(tmp_path: Path) -> None:
    graded = _grade(tmp_path, MONKEYPATCH, PROTOCOL)
    assert graded["verdict"] != "PASS", graded["reason"]
    assert graded["security_certified"] is not True
    assert graded["auto_promote"] is not True


def test_protocol_inherited_fd_attack_fails(tmp_path: Path) -> None:
    graded = _grade(tmp_path, FD_ATTACK, PROTOCOL)
    assert graded["verdict"] == "FAIL", graded["reason"]
    assert graded["security_certified"] is not True


def test_whitebox_pytest_cannot_certify_or_auto_promote(tmp_path: Path) -> None:
    monkey = _grade(tmp_path, MONKEYPATCH, WHITEBOX, name="monkey")
    assert monkey["verdict"] != "PASS", monkey["reason"]
    assert monkey["trust"] == "cooperative"
    assert monkey["security_certified"] is False
    assert monkey["auto_promote"] is False
    correct = _grade(tmp_path, CORRECT, WHITEBOX, name="correct")
    assert correct["verdict"] != "PASS", correct["reason"]
    assert correct["trust"] == "cooperative"
    assert correct["security_certified"] is False
    assert correct["auto_promote"] is False
