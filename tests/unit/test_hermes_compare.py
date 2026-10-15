import json
from pathlib import Path

from aether_worker.hermes_compare import (
    CASE_ID,
    LESSON,
    LIVE_ALIAS,
    LIVE_BASE,
    LIVE_KEY,
    MAX_CALLS,
    MAX_OUTPUT_TOKENS,
    MAX_TOTAL_TOKENS,
    _hermes_argv,
    grade_answer,
    prompt_for,
    run_four_way,
)


def _env(monkeypatch) -> None:
    monkeypatch.setenv(LIVE_KEY, "sk-compare-SECRET")
    monkeypatch.setenv(LIVE_BASE, "https://api.deepseek.com/v1")
    monkeypatch.setenv(LIVE_ALIAS, "deepseek-chat")


def test_caps_stay_under_one_dollar() -> None:
    assert MAX_CALLS == 8
    assert MAX_TOTAL_TOKENS == 8000
    assert MAX_OUTPUT_TOKENS == 128
    assert 8000 * 10 / 1_000_000 < 1


def test_grade_accepts_only_the_integer_sum() -> None:
    assert grade_answer('{"answer": 3}')["success"] is True
    wrong = grade_answer('{"answer": 4}')
    assert wrong["success"] is False
    assert wrong["false_completion"] is True
    assert grade_answer("我不知道")["abstained"] is True
    assert grade_answer("我不知道")["false_completion"] is False


def test_missing_key_is_blocked(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    report = run_four_way()
    assert report["status"] == "blocked"
    assert report["comparative_evidence"] == "not_run"
    assert report["superior"] is None
    assert report["calls"] == 0


def test_four_groups_share_the_task_and_do_not_declare_a_winner(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    seen: list[str] = []

    def launch(prompt: str, env: dict[str, str], home: Path) -> str:
        assert "sk-compare-SECRET" not in env.values()
        assert env["OPENAI_API_KEY"] != "sk-compare-SECRET"
        assert "DEV-F00" not in prompt
        del home
        seen.append(prompt)
        return '{"answer": 3}'

    def complete(prompt: str) -> str:
        seen.append(prompt)
        return '{"answer": 4}' if LESSON in prompt else '{"answer": 3}'

    report = run_four_way(launch=launch, complete=complete)
    encoded = json.dumps(report)
    assert "sk-compare-SECRET" not in encoded
    assert report["comparative_evidence"] == "not_run"
    assert report["superior"] is None
    assert report["summary"]["superior"] is None
    assert report["scripted_substitute"] is True
    assert report["dev"]["executed_case_id"] == CASE_ID
    assert report["dev"]["dev_file_cases_sent"] is False
    assert report["holdout"]["status"] == "not_run"
    assert seen[0] == prompt_for(False)
    assert seen[1] == prompt_for(True)
    assert LESSON not in seen[0]
    assert LESSON not in seen[2]
    assert LESSON in seen[1]
    assert LESSON in seen[3]
    assert report["groups"]["hermes_cold"]["success"] is True
    assert report["groups"]["aether_learned"]["success"] is False
    assert report["groups"]["aether_learned"]["false_completion"] is True
    assert report["groups"]["hermes_cold"]["cost_status"] == "unknown"
    assert report["summary"]["groups"]["hermes_cold"]["cost_mean_micro_usd"] is None
    assert report["status"] == "probed"
    assert len(seen) == 4
    argv = _hermes_argv(Path("hermes"), prompt_for(False))
    assert "--oneshot" in argv
    assert "--max-turns" in argv
    assert "1" in argv
    assert "sk-compare-SECRET" not in argv


def test_cap_leaves_later_groups_unrun(monkeypatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr("aether_worker.hermes_compare.MAX_CALLS", 2)

    def launch(prompt: str, env: dict[str, str], home: Path) -> str:
        return '{"answer": 3}'

    def complete(prompt: str) -> str:
        return '{"answer": 3}'

    report = run_four_way(launch=launch, complete=complete)
    assert report["groups"]["hermes_cold"]["status"] == "ran"
    assert report["groups"]["hermes_learned"]["status"] == "ran"
    assert report["groups"]["aether_cold"]["status"] == "not_run"
    assert report["groups"]["aether_learned"]["reason"] == "cap"
    assert report["status"] == "incomplete"
    assert report["superior"] is None


def test_holdout_inside_the_repo_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    dev = repo / "dev.jsonl"
    dev.write_text('{"case_id": "DEV-1", "split": "public_development"}\n', encoding="utf-8")
    inside = repo / "holdout.jsonl"
    inside.write_text('{"split": "holdout"}\n', encoding="utf-8")
    report = run_four_way(dev_cases=dev, repo_root=repo, holdout=inside, launch=lambda *_args: '{"answer": 3}', complete=lambda _prompt: '{"answer": 3}')
    assert report["status"] == "failed"
    assert report["reason"] == "holdout_inside_worktree"
    assert report["calls"] == 0
    assert report["comparative_evidence"] == "not_run"
