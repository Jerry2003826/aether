"""Request bodies are Pydantic models and keep the existing error codes."""

from __future__ import annotations

import uuid

import pytest

from aether_api.body_models import (
    BudgetSpec,
    CodingJobBody,
    CommandBody,
    ContractBody,
    CreateTaskBody,
    IngestionBody,
    ProjectBody,
    SourcePatchBody,
    WatchPatchBody,
)
from aether_api.console_routes import _coding_body, _search_body, _watch_patch_body
from aether_api.control import _command_body, _draft_contract
from aether_api.errors import ApiError


def test_command_and_project_validation_call_pydantic(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = CommandBody.model_validate.__func__

    def wrapped(cls, value, *args, **kwargs):
        calls.append(cls.__name__)
        return original(cls, value, *args, **kwargs)

    monkeypatch.setattr(CommandBody, "model_validate", classmethod(wrapped))
    command_id = str(uuid.uuid4())
    assert _command_body({"command_id": command_id, "kind": "pause"}) == (command_id, "pause")
    assert calls == ["CommandBody"]
    with pytest.raises(ApiError) as invalid:
        ProjectBody.model_validate({"name": "项目", "extra": 1})
    assert invalid.value.status == 422
    assert invalid.value.code == "invalid_project"
    assert invalid.value.message == "项目名称不合法"
    kept = ProjectBody.model_validate({"name": "  未裁剪  "})
    assert kept.name == "  未裁剪  "


def test_budget_bool_is_rejected_and_contract_bool_version_stays_an_int() -> None:
    with pytest.raises(ApiError) as rejected:
        BudgetSpec.model_validate({"max_tokens": True})
    assert rejected.value.code == "invalid_budget"
    project_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    body = {
        "project_id": project_id,
        "task_id": task_id,
        "goal": "目标",
        "required_artifacts": ["report"],
        "criteria": [{"criterion_id": "c1", "template": "human_review", "description": "看过"}],
        "created_at": "2026-10-16T00:00:00Z",
        "contract_version": True,
        "note": "保留",
    }
    assert _draft_contract(body, project_id, task_id) is body
    with pytest.raises(ApiError) as missing:
        IngestionBody.model_validate({"source_id": "not-a-uuid"})
    assert missing.value.status == 404
    assert missing.value.code == "not_found"
    assert missing.value.message == "任务不存在"


def test_patch_and_search_keep_only_supplied_fields() -> None:
    assert SourcePatchBody.model_validate({"status": "PAUSED"}).model_dump(exclude_unset=True) == {"status": "PAUSED"}
    assert _watch_patch_body({"interval_seconds": 300}) == {"interval_seconds": 300}
    project_id = str(uuid.uuid4())
    parsed = _search_body({"project_id": project_id, "query": "  查询  "}, project_id)
    assert parsed["query"] == "查询"
    assert parsed["mode"] == "hybrid"
    task_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    coded = _coding_body(
        {"task_id": task_id, "base_artifact_id": artifact_id, "job_key": " job ", "objective": " 做 "}
    )
    assert coded["job_key"] == "job"
    assert coded["objective"] == "做"
    goal = CreateTaskBody.model_validate({"goal": "  目标  "})
    assert goal.goal == "  目标  "
    with pytest.raises(ApiError) as bad_kind:
        CodingJobBody.model_validate({"task_id": task_id})
    assert bad_kind.value.code == "invalid_coding_job"
    contract = {"project_id": "a", "task_id": "b"}
    with pytest.raises(ApiError) as wrong_task:
        ContractBody.model_validate(contract, context={"project_id": "a", "task_id": "c"})
    assert wrong_task.value.code == "invalid_contract"
    assert wrong_task.value.message == "合同不属于这个任务"
