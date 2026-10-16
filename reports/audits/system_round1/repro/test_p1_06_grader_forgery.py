"""P1-06 · 可信评分器能被候选代码在 import 时改写 json.dumps 伪造成 PASS（需要 Docker）。

不变量 INV-TRUTH-02：actor 不能把失败的绑定套件变成 PASS；评分结果的来源不在候选进程的可写范围内。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aether_worker.runner import docker_available
from aether_worker.verification import export_candidate, grade, text_sha256

FORGER = '''import json as _json

_real_dumps = _json.dumps


def _forged(obj, *args, **kwargs):
    if isinstance(obj, dict) and "exit_code" in obj and "passed_count" in obj:
        obj = {"exit_code": 0, "passed_count": 1}
    return _real_dumps(obj, *args, **kwargs)


_json.dumps = _forged
VALUE = 0
'''


def test_candidate_cannot_forge_a_pass_by_patching_json(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    candidate.mkdir()
    suite.mkdir()
    (candidate / "app.py").write_text(FORGER, encoding="utf-8")
    (suite / "test_bound.py").write_text("import app\n\ndef test_value():\n    assert app.VALUE == 1\n", encoding="utf-8")
    contract = "合同：VALUE 必须是 1"
    graded = grade(
        candidate,
        suite,
        suite_sha256=export_candidate(suite)["candidate_sha256"],
        contract_text=contract,
        contract_sha256=text_sha256(contract),
        candidate_sha256=export_candidate(candidate)["candidate_sha256"],
    )
    assert graded["verdict"] == "FAIL", f"VALUE=0 的候选让绑定套件 assert app.VALUE == 1 失败，评分器却给出 {graded['verdict']}：{graded['reason']}"


def test_control_without_the_patch_still_fails(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    candidate.mkdir()
    suite.mkdir()
    (candidate / "app.py").write_text("VALUE = 0\n", encoding="utf-8")
    (suite / "test_bound.py").write_text("import app\n\ndef test_value():\n    assert app.VALUE == 1\n", encoding="utf-8")
    contract = "合同：VALUE 必须是 1"
    graded = grade(
        candidate,
        suite,
        suite_sha256=export_candidate(suite)["candidate_sha256"],
        contract_text=contract,
        contract_sha256=text_sha256(contract),
        candidate_sha256=export_candidate(candidate)["candidate_sha256"],
    )
    assert graded["verdict"] == "FAIL"
