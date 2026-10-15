"""Lock classification. A null version is unresolved, never a successful pin."""

from __future__ import annotations


def lock_state(value: object) -> str:
    if value is None:
        return "unlocked"
    if isinstance(value, str) and value.strip() == "":
        return "unlocked"
    return "locked"
