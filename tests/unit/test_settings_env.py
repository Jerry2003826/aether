"""Flags and non-secret settings come from pydantic-settings. Only 1 enables a flag."""

from __future__ import annotations

from pathlib import Path

import pytest

from aether_api.settings import Settings, bind_is_allowed
from aether_worker.runtime_config import intake_pause_seconds, load_runtime_settings, research_provider


def test_only_the_digit_one_enables_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AETHER_ALLOW_TESTCLIENT", "true")
    monkeypatch.setenv("AETHER_COOKIE_SECURE", "1")
    monkeypatch.setenv("AETHER_TRUST_FORWARDED_FOR", "yes")
    monkeypatch.setenv("AETHER_ORIGINS", "http://127.0.0.1:3000, http://localhost:3000")
    monkeypatch.setenv("AETHER_BOOTSTRAP_PATH", str(tmp_path / "bootstrap.json"))
    loaded = Settings()
    assert loaded.allow_testclient is False
    assert loaded.cookie_secure is True
    assert loaded.trust_forwarded_for is False
    assert loaded.origins == frozenset({"http://127.0.0.1:3000", "http://localhost:3000"})
    assert loaded.bootstrap_path == tmp_path / "bootstrap.json"
    explicit = Settings(allow_testclient=False, cookie_secure=False)
    assert explicit.allow_testclient is False
    assert explicit.cookie_secure is False
    assert type(loaded).model_config["env_prefix"] == "AETHER_"


def test_bind_still_requires_the_digit_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    certificate = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    certificate.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("AETHER_ALLOW_NON_LOOPBACK", "true")
    monkeypatch.setenv("AETHER_COOKIE_SECURE", "1")
    monkeypatch.setenv("AETHER_TLS_CERTFILE", str(certificate))
    monkeypatch.setenv("AETHER_TLS_KEYFILE", str(key))
    assert bind_is_allowed("10.0.0.8") is False
    monkeypatch.setenv("AETHER_ALLOW_NON_LOOPBACK", "1")
    assert bind_is_allowed("10.0.0.8") is True


def test_runtime_settings_keep_pause_and_provider_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_DEFER_PROGRESS", "true")
    monkeypatch.setenv("AETHER_INTAKE_PAUSE_SECONDS", "1.5")
    monkeypatch.setenv("AETHER_RESEARCH_PROVIDER", "scripted")
    monkeypatch.setenv("AETHER_TASK_QUEUE", "queue-b")
    loaded = load_runtime_settings()
    assert loaded.defer_progress is False
    assert intake_pause_seconds(loaded.intake_pause_seconds) == 1.5
    assert research_provider(loaded.research_provider) == "scripted"
    assert loaded.task_queue == "queue-b"
    monkeypatch.setenv("AETHER_DEFER_PROGRESS", "1")
    assert load_runtime_settings().defer_progress is True
    with pytest.raises(SystemExit, match="AETHER_INTAKE_PAUSE_SECONDS 必须是非负秒数"):
        intake_pause_seconds("-1")
    with pytest.raises(SystemExit, match="AETHER_RESEARCH_PROVIDER 只能是 live 或 scripted"):
        research_provider("other")
