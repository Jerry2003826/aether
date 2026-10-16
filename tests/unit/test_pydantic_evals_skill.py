"""Skill cases are scheduled by pydantic-evals. grade() still scores them."""

from __future__ import annotations

import time

import pytest
from pydantic_evals import Dataset

from aether_worker.skill_eval import SkillEvalError
from aether_worker.skill_execute import _evaluate_cases


def _case(name: str) -> dict:
    return {"family": name, "suite_files": {"test_value.py": "def test_value():\n    return None\n"}}


def test_dataset_evaluate_sync_schedules_each_case(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int, int]] = []
    grades: list[dict] = []
    original = Dataset.evaluate_sync

    def wrapped(self, task, *args, **kwargs):
        seen.append((self.name, kwargs["max_concurrency"], len(self.cases)))
        return original(self, task, *args, **kwargs)

    def grade(*_args, **_kwargs):
        grades.append({"verdict": "PASS"})
        return {"verdict": "PASS", "sentinel_visible": False, "credential_present": False}

    monkeypatch.setattr(Dataset, "evaluate_sync", wrapped)
    monkeypatch.setattr("aether_worker.skill_execute.export_candidate", lambda _path: {"candidate_sha256": "a" * 64})
    monkeypatch.setattr("aether_worker.skill_execute.text_sha256", lambda _text: "b" * 64)
    monkeypatch.setattr("aether_worker.skill_execute.grade", grade)
    result = _evaluate_cases([_case("one"), _case("two")], {"candidate.py": "x = 1\n"}, "合同", None)
    assert seen == [("aether-skill-grade", 1, 2)]
    assert len(grades) == 2
    assert [item["verdict"] for item in result] == ["PASS", "PASS"]


def test_expired_deadline_raises_before_a_second_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"grade": 0}

    def grade(*_args, **_kwargs):
        calls["grade"] += 1
        return {"verdict": "PASS"}

    monkeypatch.setattr("aether_worker.skill_execute.export_candidate", lambda _path: {"candidate_sha256": "a" * 64})
    monkeypatch.setattr("aether_worker.skill_execute.grade", grade)
    with pytest.raises(SkillEvalError) as rejected:
        _evaluate_cases([_case("one"), _case("two")], {"candidate.py": "x = 1\n"}, "合同", time.monotonic() - 1)
    assert rejected.value.code == "deadline_exceeded"
    assert calls["grade"] == 0
