"""Process settings. Secrets are paths or environment values, never hardcoded passwords."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _only_one(value: object) -> bool:
    """Only the character 1 enables a flag. Constructor booleans are left as given."""
    if isinstance(value, bool):
        return value
    return str(value) == "1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AETHER_", extra="ignore", frozen=True)

    database_url: str | None = None
    bootstrap_path: Path = Path("secrets/bootstrap.json")
    allow_testclient: bool = False
    origins: Annotated[frozenset[str], NoDecode] = frozenset({"http://127.0.0.1:3000", "http://localhost:3000"})
    cookie_secure: bool = False
    artifact_root: Path = Path("data/artifacts")
    index_root: Path = Path("data/indexes")
    # Authentication limits must be positive (Codex r4309229029): 0 or less would lock every login out
    # (failure limit), create sessions that are already expired (hours, idle) or disable throttling
    # (window), so such a value fails at startup instead.
    session_hours: Annotated[int, Field(gt=0)] = 12
    idle_minutes: Annotated[int, Field(gt=0)] = 30
    login_failure_limit: Annotated[int, Field(gt=0)] = 5
    login_window_seconds: Annotated[int, Field(gt=0)] = 900
    trust_forwarded_for: bool = False

    @field_validator("allow_testclient", "cookie_secure", "trust_forwarded_for", mode="before")
    @classmethod
    def strict_flag(cls, value: object) -> bool:
        return _only_one(value)

    @field_validator("origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> frozenset[str]:
        if isinstance(value, frozenset):
            return value
        if isinstance(value, (set, list, tuple)):
            return frozenset(str(item).strip() for item in value if str(item).strip())
        text = "" if value is None else str(value)
        return frozenset(item.strip() for item in text.split(",") if item.strip())

    @classmethod
    def from_env(cls) -> Settings:
        return cls()


def bind_is_allowed(host: str) -> bool:
    if host in {"127.0.0.1", "::1"}:
        return True
    if os.environ.get("AETHER_ALLOW_NON_LOOPBACK") != "1":
        return False
    if os.environ.get("AETHER_COOKIE_SECURE") != "1":
        return False
    certificate = os.environ.get("AETHER_TLS_CERTFILE", "")
    key = os.environ.get("AETHER_TLS_KEYFILE", "")
    return bool(certificate and key and Path(certificate).is_file() and Path(key).is_file())
