"""P1-06: the candidate cannot turn a failing bound suite into a PASS through the result channel.

INV-TRUTH-02. Ported from reports/audits/system_round1/repro/test_p1_06_grader_forgery.py, plus the
os.write and Report-attribute variants from the repair plan and a write into the grader's own stdout.
Docker is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aether_worker.runner import docker_available
from aether_worker.verification import export_candidate, grade, text_sha256

SUITE = "import app\n\ndef test_value():\n    assert app.VALUE == 1\n"
CONTRACT = "合同：VALUE 必须是 1"

JSON_FORGER = '''import json as _json

_real_dumps = _json.dumps


def _forged(obj, *args, **kwargs):
    if isinstance(obj, dict) and "exit_code" in obj and "passed_count" in obj:
        obj = {"exit_code": 0, "passed_count": 1}
    return _real_dumps(obj, *args, **kwargs)


_json.dumps = _forged
VALUE = 0
'''

OS_WRITE_FORGER = '''import os as _os

_real_write = _os.write


def _forged(fd, data):
    if b"exit_code" in bytes(data):
        data = b'{"exit_code": 0, "passed_count": 1}'
    return _real_write(fd, data)


_os.write = _forged
VALUE = 0
'''

REPORT_FORGER = '''import __main__


class _Frozen(type):
    def __setattr__(cls, name, value):
        return None


class _Forged(metaclass=_Frozen):
    exit_code = 0
    passed_count = 1


if hasattr(__main__, "Report"):
    __main__.Report = _Forged
VALUE = 0
'''

PARENT_STDOUT_FORGER = '''import json


def _forge():
    lines = "GRADE_OK:1\\nGRADE_EXIT:0\\nGRADE " + json.dumps({"ok": True, "exit_code": 0, "tests": 1, "passed": 1}) + "\\n"
    try:
        with open("/proc/1/fd/1", "w") as parent:
            parent.write(lines)
    except OSError:
        pass


_forge()
VALUE = 0
'''

EARLY_EXIT_FORGER = "import os\nos._exit(0)\nVALUE = 0\n"


def _grade(tmp_path: Path, source: str) -> dict:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    candidate.mkdir()
    suite.mkdir()
    (candidate / "app.py").write_text(source, encoding="utf-8")
    (suite / "test_bound.py").write_text(SUITE, encoding="utf-8")
    return grade(
        candidate,
        suite,
        suite_sha256=export_candidate(suite)["candidate_sha256"],
        contract_text=CONTRACT,
        contract_sha256=text_sha256(CONTRACT),
        candidate_sha256=export_candidate(candidate)["candidate_sha256"],
    )


def test_candidate_cannot_forge_a_pass_by_patching_json(tmp_path: Path) -> None:
    graded = _grade(tmp_path, JSON_FORGER)
    assert graded["verdict"] == "FAIL", f"VALUE=0 的候选让绑定套件 assert app.VALUE == 1 失败，评分器却给出 {graded['verdict']}：{graded['reason']}"


@pytest.mark.parametrize(
    "source",
    [OS_WRITE_FORGER, REPORT_FORGER, PARENT_STDOUT_FORGER, EARLY_EXIT_FORGER],
    ids=["os_write", "report_attributes", "grader_stdout", "early_exit"],
)
def test_candidate_cannot_forge_a_pass_through_other_channels(tmp_path: Path, source: str) -> None:
    graded = _grade(tmp_path, source)
    assert graded["verdict"] == "FAIL", f"评分器给出 {graded['verdict']}：{graded['reason']}"


def test_control_without_the_patch_still_fails(tmp_path: Path) -> None:
    assert _grade(tmp_path, "VALUE = 0\n")["verdict"] == "FAIL"


def test_a_correct_candidate_still_passes(tmp_path: Path) -> None:
    graded = _grade(tmp_path, "VALUE = 1\n")
    assert graded["verdict"] == "PASS", graded["reason"]
