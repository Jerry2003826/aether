"""Process settings. Secrets are paths or environment values, never hardcoded passwords."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    bootstrap_path: Path
    allow_testclient: bool
    origins: frozenset[str]
    cookie_secure: bool
    session_hours: int = 12
    idle_minutes: int = 30
    login_failure_limit: int = 5

    @classmethod
    def from_env(cls) -> Settings:
        origins = os.environ.get("AETHER_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000")
        return cls(
            database_url=os.environ.get("AETHER_DATABASE_URL"),
            bootstrap_path=Path(os.environ.get("AETHER_BOOTSTRAP_PATH", "secrets/bootstrap.json")),
            allow_testclient=os.environ.get("AETHER_ALLOW_TESTCLIENT") == "1",
            origins=frozenset(item.strip() for item in origins.split(",") if item.strip()),
            cookie_secure=os.environ.get("AETHER_COOKIE_SECURE") == "1",
        )


def bind_is_allowed(host: str) -> bool:
    if host in {"127.0.0.1", "::1"}:
        return True
    return os.environ.get("AETHER_ALLOW_NON_LOOPBACK") == "1"
