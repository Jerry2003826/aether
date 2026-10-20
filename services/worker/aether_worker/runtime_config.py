"""Non-secret process settings. Verifier URLs and provider keys stay out of this model."""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _only_one(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip() == "1"


class RuntimeSettings(BaseSettings):
    """Worker settings that are not credentials. Missing env uses the same defaults as before."""

    model_config = SettingsConfigDict(env_prefix="AETHER_", extra="ignore")

    index_root: Path = Path("data/indexes")
    artifact_root: Path = Path("data/artifacts")
    defer_progress: bool = False
    intake_pause_seconds: str = "0"
    research_provider: str = "live"
    task_queue: str = "aether-tasks"
    live_model_base: str = ""
    live_model_alias: str = ""
    verifier_base: str = ""
    gateway_base: str = ""
    runner_base: str = ""

    @field_validator("defer_progress", mode="before")
    @classmethod
    def strict_flag(cls, value: object) -> bool:
        return _only_one(value)

    @field_validator("intake_pause_seconds", "research_provider", "live_model_base", "live_model_alias", mode="before")
    @classmethod
    def as_text(cls, value: object) -> str:
        return "" if value is None else str(value)


def load_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()


def intake_pause_seconds(raw: str) -> float:
    text = raw.strip()
    if text == "":
        return 0.0
    try:
        value = float(text)
    except ValueError as exc:
        raise SystemExit("AETHER_INTAKE_PAUSE_SECONDS 必须是非负秒数") from exc
    if value < 0:
        raise SystemExit("AETHER_INTAKE_PAUSE_SECONDS 必须是非负秒数")
    return value


def research_provider(raw: str) -> str:
    text = raw.strip().lower()
    if text in {"", "live"}:
        return "live"
    if text == "scripted":
        return "scripted"
    raise SystemExit("AETHER_RESEARCH_PROVIDER 只能是 live 或 scripted")
