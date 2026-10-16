"""Threat-browser visits keep an audit row after the caller drops its list."""

from __future__ import annotations

import ipaddress
import json

from sqlalchemy import text

from aether_storage.db import Database
from aether_worker import threat_browser
from aether_worker.threat_browser import Switches, VisitRequest
from aether_worker.threat_pins import CHROMIUM_BROWSER_VERSION, ENTRYPOINT, PLAYWRIGHT_VERSION
from aether_worker.threat_visit import visit_threat_page
from tests.integration.test_console_api import _session

BROWSER = Switches(browser_enabled=True, tor_enabled=False, onion_enabled=False, owner_confirmed=False)
_BROWSER_IMAGE = "sha256:" + ("ab" * 32)
_TOR_IMAGE = "sha256:" + ("cd" * 32)


def test_visit_audit_remains_after_the_caller_drops_the_list(client, role_urls, monkeypatch) -> None:
    monkeypatch.setenv("AETHER_WORKER_DATABASE_URL", role_urls["aether_worker"])
    monkeypatch.setattr(threat_browser, "default_switches", lambda path=None: BROWSER)
    owner_id, project_id, _csrf = _session(client, "threat-audit")
    worker = Database(role_urls["aether_worker"])
    audit: list[dict[str, object]] = []

    def runner(args: list[str], _stdin: bytes) -> bytes:
        if ENTRYPOINT in args:
            body = {
                "text": "可见正文",
                "browser_version": CHROMIUM_BROWSER_VERSION,
                "playwright_version": PLAYWRIGHT_VERSION,
                "user_agent": "HeadlessChrome",
            }
            return json.dumps(body).encode()
        return b""

    def resolve(_host: str) -> list[ipaddress.IPv4Address]:
        return [ipaddress.ip_address("93.184.216.34")]

    def allow(_request: VisitRequest, _digest: str) -> str:
        return owner_id

    for path in ("/one", "/two"):
        request = VisitRequest(
            url=f"https://example.com{path}",
            switches=BROWSER,
            deployed=BROWSER,
            runtime="container",
            browser_image=_BROWSER_IMAGE,
            tor_image=_TOR_IMAGE,
            project_id=project_id,
        )
        visit_threat_page(request, audit=audit, resolve=resolve, runner=runner, cleanup=runner, authorize=allow)
    assert len(audit) == 2
    audit.clear()
    with worker.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text(
                "SELECT sequence, event_hash, previous_hash, action, payload_redacted "
                "FROM audit_events WHERE project_id = :project_id ORDER BY sequence"
            ),
            {"project_id": project_id},
        ).mappings().all()
    assert len(rows) == 2
    assert rows[0]["action"] == "threat_browser.visit"
    assert rows[0]["previous_hash"] is None
    assert rows[1]["previous_hash"] == rows[0]["event_hash"]
    second = rows[1]["payload_redacted"]
    if isinstance(second, str):
        second = json.loads(second)
    assert second["previous_hash"] == rows[0]["event_hash"]
    assert second["admissible_evidence"] is False
    assert second["host"] == "example.com"
