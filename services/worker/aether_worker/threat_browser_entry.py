"""Container entrypoint. It calls the same Playwright reader and does not parse HTML itself."""

from __future__ import annotations

import json
import sys
from typing import Any

from aether_worker.net_guard import fixtures_enabled, resolve_host
from aether_worker.threat_browser import Switches, ThreatBrowserError, VisitRequest, enforce_policy, read_page
from aether_worker.threat_pins import APPROVED_TOR_PROXY


def run_entry(payload: dict[str, Any]) -> dict[str, str]:
    switches = _switches(payload)
    if not fixtures_enabled():
        enforce_policy(VisitRequest(url=str(payload.get("url") or ""), switches=switches, deployed=Switches()))
    proxy = _proxy(payload.get("proxy"), switches)
    capture = read_page(
        str(payload["url"]),
        switches=switches,
        allow_hosts=_allow_hosts(payload),
        resolve=resolve_host,
        proxy=proxy,
    )
    return {
        "text": capture.text,
        "browser_version": capture.browser_version,
        "playwright_version": capture.playwright_version,
        "user_agent": capture.user_agent,
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ThreatBrowserError("container_unavailable")
        json.dump(run_entry(payload), sys.stdout)
    except ThreatBrowserError as exc:
        _print_cause(exc)
        json.dump({"error": exc.code}, sys.stdout)
        raise SystemExit(1) from exc


def _print_cause(exc: BaseException) -> None:
    current: BaseException | None = exc
    for _ in range(6):
        if current is None:
            return
        nxt = current.__cause__ or current.__context__
        if nxt is None:
            return
        print(f"{type(nxt).__name__}: {nxt}", file=sys.stderr)
        current = nxt


def _allow_hosts(payload: dict[str, Any]) -> frozenset[str] | None:
    if not fixtures_enabled():
        return None
    raw = payload.get("allow_hosts")
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return None
    return frozenset(raw)


def _switches(payload: dict[str, Any]) -> Switches:
    return Switches(
        browser_enabled=bool(payload.get("browser_enabled")),
        tor_enabled=bool(payload.get("tor_enabled")),
        onion_enabled=bool(payload.get("onion_enabled")),
        owner_confirmed=bool(payload.get("owner_confirmed")),
    )


def _proxy(proxy: object, switches: Switches) -> str | None:
    if not switches.tor_enabled:
        if proxy not in {None, ""}:
            raise ThreatBrowserError("proxy_rejected")
        return None
    if proxy != APPROVED_TOR_PROXY:
        raise ThreatBrowserError("proxy_rejected")
    return APPROVED_TOR_PROXY


if __name__ == "__main__":
    main()
