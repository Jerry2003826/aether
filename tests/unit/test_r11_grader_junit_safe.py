"""The grader container parses pytest JUnit without DTDs or entities; the pass rules stay the same."""

from __future__ import annotations

import ast
from typing import Any

import pytest
from aether_worker.grader_runner import GRADER_PROGRAM

REQUIRED = {("test_mod", "test_a")}


def _judge() -> Any:
    tree = ast.parse(GRADER_PROGRAM)
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"parse_report", "judge_report"}]
    assert {node.name for node in found} == {"parse_report", "judge_report"}
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(body=[*imports, *found], type_ignores=[]), "grader", "exec"), namespace)  # noqa: S102
    return namespace["judge_report"]


def _suite(cases: str) -> bytes:
    return f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>'.encode()


_BOMB = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<testsuites><testsuite><testcase classname="test_mod" name="test_a">&lol3;</testcase></testsuite></testsuites>"""

_EXTERNAL = b"""<?xml version="1.0"?>
<!DOCTYPE r [<!ENTITY ext SYSTEM "file:///etc/passwd">]>
<testsuites><testsuite><testcase classname="test_mod" name="test_a">&ext;</testcase></testsuite></testsuites>"""

_DTD_ONLY = b"""<?xml version="1.0"?>
<!DOCTYPE testsuites SYSTEM "http://127.0.0.1:9/junit.dtd">
<testsuites><testsuite><testcase classname="test_mod" name="test_a"/></testsuite></testsuites>"""


@pytest.mark.parametrize("payload", [_BOMB, _EXTERNAL, _DTD_ONLY], ids=["entity_expansion", "external_entity", "external_dtd"])
def test_junit_with_dtd_or_entities_is_rejected(payload: bytes) -> None:
    assert _judge()(payload, REQUIRED, 0)["reason"] == "report_unreadable"


def test_clean_junit_still_passes() -> None:
    verdict = _judge()(_suite('<testcase classname="test_mod" name="test_a" time="0.01"/>'), REQUIRED, 0)
    assert verdict == {"ok": True, "reason": "passed", "exit_code": 0, "tests": 1}


def test_duplicate_testcase_rejected() -> None:
    cases = '<testcase classname="test_mod" name="test_a"/><testcase classname="test_mod" name="test_a"><failure message="x"/></testcase>'
    assert _judge()(_suite(cases), REQUIRED, 0)["ok"] is False


def test_zero_executed_rejected() -> None:
    assert _judge()(_suite(""), REQUIRED, 0)["reason"] == "required_test_missing"


def test_all_skipped_rejected() -> None:
    cases = '<testcase classname="test_mod" name="test_a"><skipped message="s"/></testcase>'
    assert _judge()(_suite(cases), REQUIRED, 0)["reason"] == "test_not_passed"


def test_grader_program_has_no_unguarded_elementtree_parse() -> None:
    assert GRADER_PROGRAM.count("ElementTree.fromstring(") == 1
    assert "StartDoctypeDeclHandler" in GRADER_PROGRAM
