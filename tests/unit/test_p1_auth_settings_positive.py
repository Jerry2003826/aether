"""Authentication settings must be positive (Codex r4309229029).

AETHER_LOGIN_FAILURE_LIMIT=0 made every login answer 429, AETHER_SESSION_HOURS=0 created sessions (and
cookies) that were already expired, and a zero or negative idle or window value disabled the idle
timeout or the login throttle. Each is now refused when the settings are loaded, so the API fails at
startup instead of silently locking users out or weakening rate limiting.
"""

from __future__ import annotations

import pytest
from aether_api.settings import Settings
from pydantic import ValidationError

_FIELDS = ("session_hours", "idle_minutes", "login_failure_limit", "login_window_seconds")


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_nonpositive_environment_value_is_refused(monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    monkeypatch.setenv("AETHER_" + field.upper(), value)
    with pytest.raises(ValidationError) as raised:
        Settings()
    assert [error["loc"] for error in raised.value.errors()] == [(field,)]
    assert raised.value.errors()[0]["type"] == "greater_than"


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("value", [0, -5])
def test_a_nonpositive_constructor_value_is_refused(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings(**{field: value})
    assert [error["loc"] for error in raised.value.errors()] == [(field,)]


@pytest.mark.parametrize("field", _FIELDS)
def test_one_is_accepted(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv("AETHER_" + field.upper(), "1")
    assert getattr(Settings(), field) == 1


def test_the_defaults_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in _FIELDS:
        monkeypatch.delenv("AETHER_" + field.upper(), raising=False)
    loaded = Settings()
    assert (loaded.session_hours, loaded.idle_minutes, loaded.login_failure_limit, loaded.login_window_seconds) == (12, 30, 5, 900)


def test_the_app_does_not_start_with_a_zero_login_failure_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_api.app import create_app

    monkeypatch.setenv("AETHER_LOGIN_FAILURE_LIMIT", "0")
    with pytest.raises(ValidationError):
        create_app()
