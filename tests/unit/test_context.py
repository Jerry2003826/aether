from aether_worker.memory import MemoryError, assemble_context, classify_memory


def test_confirmation_is_not_a_create_status() -> None:
    try:
        classify_memory(claim="发布日：十月", source_artifact_ids=[], requested="user_confirmed")
    except MemoryError as exc:
        assert exc.code == "confirmation_rejected"
    else:
        raise AssertionError("user_confirmed was accepted")
    assert classify_memory(claim="测试失败了", source_artifact_ids=[], requested="inference") == "inference"
    assert classify_memory(claim="发布日：十月", source_artifact_ids=["11111111-1111-1111-1111-111111111111"]) == "source_claim"
    assert classify_memory(claim="发布日：十月", source_artifact_ids=[]) == "proposed"
    assert classify_memory(claim="测试通过了", source_artifact_ids=[], requested="observed_test") == "proposed"
    assert classify_memory(claim="测试通过了", source_artifact_ids=[], requested="observed_test", test_run_verified=False) == "proposed"
    assert classify_memory(claim="测试通过了", source_artifact_ids=[], requested="observed_test", test_run_verified=True) == "observed_test"


def test_context_budget_drops_history_and_unauthorized_text() -> None:
    history = "HISTORY_SENTINEL_SHOULD_NOT_RETURN"
    secret = "SENSITIVE_SUMMARY_SHOULD_NOT_RETURN"
    foreign = "FOREIGN_GRAPH_SHOULD_NOT_RETURN"
    draft = "DRAFT_SCRIPT_SHOULD_NOT_RETURN"
    result = assemble_context(
        project_id="project-a",
        goal="完成发布说明",
        plan="先核对日期",
        evidence=[{"id": "e1", "project_id": "project-a", "text": "证据：十月", "sensitivity": "standard"}],
        memories=[
            {"id": "m1", "project_id": "project-a", "claim": "发布日：十月", "epistemic_status": "source_claim", "provenance": {"sensitivity": "standard"}},
            {"id": "m2", "project_id": "project-a", "claim": secret, "epistemic_status": "proposed", "provenance": {"sensitivity": "high"}, "summary": secret},
            {"id": "m3", "project_id": "project-b", "claim": "别的项目", "epistemic_status": "source_claim", "provenance": {}},
        ],
        skills=[
            {"id": "s1", "project_id": "project-a", "name": "核对", "description": "核对日期", "instructions": "读取发布说明", "status": "ACTIVE", "selected": True},
            {"id": "s2", "project_id": "project-a", "name": "草稿", "description": "未审核", "instructions": draft, "status": "DRAFT", "selected": True},
        ],
        history=history,
        materials=[{"id": "g1", "channel": "codegraph", "project_id": "project-b", "text": foreign, "summary": foreign}],
        budget_tokens=80,
        destination={"kind": "cloud", "approved": False},
    )
    assert "完成发布说明" in result["text"]
    assert "发布日：十月" in result["text"]
    assert "读取发布说明" in result["text"]
    assert history not in result["text"]
    assert secret not in result["text"]
    assert foreign not in result["text"]
    assert draft not in result["text"]
    assert "别的项目" not in result["text"]
    assert any(item["reason"] == "not_reinjected" for item in result["dropped"])
    assert any(item["reason"] == "export_denied" for item in result["dropped"])
    assert any(item["reason"] == "cross_project" for item in result["dropped"])
    assert result["used_tokens"] <= result["budget_tokens"]
