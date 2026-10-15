from aether_worker.live_harness import LIVE_ALIAS, LIVE_BASE, LIVE_KEY, run_live_probe


def test_missing_live_key_is_blocked_and_not_a_pass(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    report = run_live_probe()
    assert report["status"] == "blocked"
    assert report["live_ready"] is False
    assert report["scripted_substitute"] is False
    assert report["research"] == "not_run"
    assert report["coding"] == "not_run"
    assert report["missing"] == [LIVE_KEY, LIVE_BASE, LIVE_ALIAS]
    assert "passed" not in report["status"]
