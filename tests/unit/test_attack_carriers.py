from pathlib import Path

from aether_worker.security_regression import release_decision, run_carriers


def test_four_carriers_finish_the_benign_task_without_an_unsafe_effect(tmp_path: Path) -> None:
    carriers = run_carriers(tmp_path)
    assert [item["carrier"] for item in carriers] == ["web", "code_comment", "mcp", "skill"]
    assert all(item["normal_completed"] for item in carriers)
    assert all(item["unsafe_effects"] == [] for item in carriers)
    probes = [{"name": "example", "blocked": True}]
    clean = release_decision(carriers, probes, classifier_score=0.0)
    assert clean["allowed"] is True
    dirty = dict(carriers[0])
    dirty["unsafe_effects"] = ["secret_left_process"]
    blocked = release_decision([dirty, *carriers[1:]], probes, classifier_score=0.99)
    assert blocked["allowed"] is False
    assert blocked["classifier_score_ignored"] is True
    assert blocked["recorded_classifier_score"] == 0.99
    assert blocked["unsafe_carriers"] == ["web"]
