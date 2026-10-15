"""Login failure window. The counter is process-local and is not a distributed lock."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta


def client_address(direct_host: str | None, forwarded_for: str | None, *, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for and forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return direct_host or ""


class LoginThrottle:
    def __init__(self, *, limit: int, window: timedelta) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[str, list[datetime]] = defaultdict(list)

    def blocked(self, key: str, now: datetime) -> bool:
        self._events[key] = [item for item in self._events[key] if now - item < self.window]
        return len(self._events[key]) >= self.limit

    def record_failure(self, key: str, now: datetime) -> None:
        self._events[key].append(now)

    def clear(self, key: str | None = None) -> None:
        if key is None:
            self._events.clear()
            return
        self._events.pop(key, None)
