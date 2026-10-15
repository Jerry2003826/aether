"""Login failure window. The counter is process-local and is not a distributed lock."""

from __future__ import annotations

import threading
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
        self._lock = threading.Lock()

    def acquire(self, key: str, now: datetime) -> bool:
        """Reserve one failure slot before the password check. False means the window is full."""
        with self._lock:
            self._prune(key, now)
            if len(self._events[key]) >= self.limit:
                return False
            self._events[key].append(now)
            return True

    def release(self, key: str) -> None:
        """Return a slot reserved for an attempt that was not a credential failure."""
        with self._lock:
            events = self._events.get(key)
            if events:
                events.pop()

    def clear(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
                return
            self._events.pop(key, None)

    def _prune(self, key: str, now: datetime) -> None:
        self._events[key] = [item for item in self._events[key] if now - item < self.window]
