"""SYS-02: protocol extraction keeps expected values out of the candidate document."""

from __future__ import annotations

import ast
from pathlib import Path

from aether_worker.grader_protocol import extract_protocol, probes_document, values_match
from aether_worker.grader_runner import GRADER_PROGRAM, PROTOCOL_CHILD_SOURCE
from aether_worker.verification import _recordable, allows_auto_promotion


def _tree(root: Path, candidate: str, suite: str) -> tuple[Path, Path]:
    source = root / "candidate"
    tests = root / "suite"
    source.mkdir()
    tests.mkdir()
    (source / "candidate.py").write_text(candidate, encoding="utf-8")
    (tests / "test_trusted.py").write_text(suite, encoding="utf-8")
    return source, tests


def test_equality_assertion_is_a_protocol_without_expected_values(tmp_path: Path) -> None:
    source, tests = _tree(
        tmp_path,
        "def value():\n    return 0\n",
        "import candidate\n\ndef test_required_result():\n    assert candidate.value() == 42\n",
    )
    protocol = extract_protocol(source, tests)
    assert protocol is not None
    assert protocol[0].expected == 42
    document = probes_document(protocol)
    assert "expected" not in document["cases"][0]
    assert values_match(protocol, [{"id": protocol[0].case_id, "value": 42}])
    assert not values_match(protocol, [{"id": protocol[0].case_id, "value": 0}])


def test_attribute_assertion_is_a_protocol(tmp_path: Path) -> None:
    source = tmp_path / "candidate"
    tests = tmp_path / "suite"
    source.mkdir()
    tests.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_bound.py").write_text("import app\n\ndef test_value():\n    assert app.VALUE == 1\n", encoding="utf-8")
    protocol = extract_protocol(source, tests)
    assert protocol is not None
    assert protocol[0].kind == "attr"
    assert protocol[0].attr == ("VALUE",)
    assert protocol[0].expected == 1


def test_assignment_before_assert_is_cooperative_whitebox(tmp_path: Path) -> None:
    source, tests = _tree(
        tmp_path,
        "def value():\n    return 42\n",
        "import candidate\n\ndef test_required_result():\n    seen = candidate.value()\n    assert seen == 42\n",
    )
    assert extract_protocol(source, tests) is None


def test_decorator_is_not_a_protocol(tmp_path: Path) -> None:
    source, tests = _tree(
        tmp_path,
        "VALUE = 1\n",
        "import pytest\nimport candidate\n\n@pytest.mark.skip\ndef test_value():\n    assert candidate.VALUE == 1\n",
    )
    assert extract_protocol(source, tests) is None


def test_grader_program_keeps_namespace_cleanup_inside_the_container_source() -> None:
    assert GRADER_PROGRAM.count("os.kill(-1, signal.SIGKILL)") == 1
    ast.parse(GRADER_PROGRAM)
    ast.parse(PROTOCOL_CHILD_SOURCE)
    assert "expected" not in PROTOCOL_CHILD_SOURCE


def test_cooperative_and_uncertified_results_cannot_promote() -> None:
    cooperative = {"verdict": "PASS", "trust": "cooperative", "security_certified": False, "auto_promote": True}
    isolated = {"verdict": "PASS", "trust": "isolated", "security_certified": True, "auto_promote": False}
    forged = {"verdict": "PASS", "trust": "isolated", "security_certified": True, "auto_promote": True}
    assert allows_auto_promotion(cooperative) is False
    assert allows_auto_promotion(isolated) is False
    assert allows_auto_promotion(forged) is False
    assert _recordable(cooperative) is False
    assert _recordable(isolated) is True
    assert _recordable(forged) is False
