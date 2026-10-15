from pathlib import Path

from aether_worker.hermes_baseline import LIVE_KEY, experiment_plan, probe_cli, summarize, wilson_interval


def test_missing_hermes_cli_is_not_a_version(monkeypatch) -> None:
    monkeypatch.setattr("aether_worker.hermes_baseline.shutil.which", lambda _name: None)
    probed = probe_cli()
    assert probed["status"] == "not_run"
    assert probed["version"] is None


def test_plan_keeps_dev_and_holdout_apart_and_does_not_claim_a_winner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    dev = repo / "dev_cases.jsonl"
    dev.write_text('{"case_id": "DEV-1", "split": "public_development"}\n', encoding="utf-8")
    inside = repo / "holdout.jsonl"
    inside.write_text("{}\n", encoding="utf-8")
    plan = experiment_plan(dev, repo_root=repo, holdout=inside)
    assert plan["dev"]["count"] == 1
    assert plan["dev"]["split"] == "public_development"
    assert plan["holdout"]["reason"] == "holdout_inside_worktree"
    assert plan["live"] == "not_run"
    assert plan["missing_live_key"] == LIVE_KEY
    assert plan["superior"] is None
    assert {item["budget"]["max_cost_micro_usd"] for item in plan["groups"]} == {2_000_000}
    outside = tmp_path / "sealed.jsonl"
    outside.write_text("[]\n", encoding="utf-8")
    sealed = experiment_plan(dev, repo_root=repo, holdout=outside)
    assert sealed["holdout"]["status"] == "sealed"
    summary = summarize(
        {
            "hermes_cold": [{"success": True, "cost_status": "unknown"}],
            "hermes_learned": [],
            "aether_cold": [{"success": False, "cost_status": "known", "cost_micro_usd": 10}],
            "aether_learned": [{"success": True, "cost_status": "known", "cost_micro_usd": 12}],
        }
    )
    assert summary["superior"] is None
    assert summary["unknown_costs_dropped"] is False
    assert summary["groups"]["hermes_cold"]["cost_mean_micro_usd"] is None
    assert summary["groups"]["aether_cold"]["wilson95"] is not None
    low, high = wilson_interval(1, 1)
    assert low == summary["groups"]["aether_learned"]["wilson95"][0]
    assert high == summary["groups"]["aether_learned"]["wilson95"][1]
    real = experiment_plan(Path("evals/dev_cases.jsonl"), repo_root=Path("."))
    assert real["dev"]["count"] == 28
    assert real["live"] == "not_run"
    assert real["missing_live_key"] == LIVE_KEY
    assert real["superior"] is None
    assert real["cli"]["status"] in {"not_run", "probed", "failed"}
    if real["cli"]["status"] != "probed":
        assert real["cli"]["version"] is None
