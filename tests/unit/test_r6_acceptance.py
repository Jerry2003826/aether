"""Frozen acceptance criteria are not replaced by the automatic goal check."""

from aether_worker.goal_effect import _acceptance_outcome
from aether_worker.memory import _lowers_sensitivity, _result_records_failure


def test_human_review_is_not_an_automatic_pass() -> None:
    content = {
        "goal": "核对",
        "criteria": [
            {
                "criterion_id": "owner_review",
                "template": "human_review",
                "description": "所有者确认后才能完成",
                "mandatory": True,
                "parameters": {"rubric": "核对"},
            }
        ],
    }
    state, detail = _acceptance_outcome(content, answer="一百摄氏度".encode(), sources=["事实"], goal="核对")
    assert state == "unverifiable"
    assert detail


def test_automatic_contract_has_no_extra_acceptance_gate() -> None:
    state, detail = _acceptance_outcome({"criterion_id": "goal_artifact", "goal": "核对"}, answer=b"ok", sources=[], goal="核对")
    assert state == "pass"
    assert detail == ""


def test_artifact_exists_uses_the_frozen_minimum() -> None:
    content = {
        "criteria": [
            {
                "criterion_id": "bytes",
                "template": "artifact_exists",
                "description": "回答要有足够正文",
                "parameters": {"logical_name": "answer.txt", "minimum_bytes": 5},
            }
        ]
    }
    assert _acceptance_outcome(content, answer=b"hi", sources=[], goal="核对")[0] == "fail"
    assert _acceptance_outcome(content, answer=b"hello", sources=[], goal="核对")[0] == "pass"


def test_failed_result_bytes_are_not_a_passing_run() -> None:
    assert _result_records_failure(b'{"kind":"test_run","status":"failed","exit_code":99}') is True
    assert _result_records_failure(b'{"kind":"test_run","status":"passed","exit_code":0}') is False


def test_only_a_lower_rank_is_declassification() -> None:
    assert _lowers_sensitivity("high", "standard") is True
    assert _lowers_sensitivity("high", "high") is False
    assert _lowers_sensitivity("standard", "high") is False
