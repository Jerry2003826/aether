"""Worker-side threat visit. The container image does not import this module."""

from __future__ import annotations

import os

from aether_storage.db import Database
from aether_worker.net_guard import fixtures_enabled, resolve_host
from aether_worker.threat_audit import persist_threat_audit
from aether_worker.threat_browser import (
    PageCapture,
    Resolver,
    ThreatBrowserError,
    ThreatDocument,
    VisitRequest,
    append_audit,
    authorize_target,
    chosen_proxy,
    enforce_policy,
    intent_digest,
    read_page,
    source_label,
    visit_audit,
)
from aether_worker.threat_browser import _MARKER, _sanitize_capture
from aether_worker.threat_grant import GrantError, worker_authorize
from aether_worker.threat_launch import Runner, ThreatLaunchError, execute_container, remote_execute
from aether_worker.threat_pins import CHROMIUM_BROWSER_VERSION, PLAYWRIGHT_VERSION

_WORKER = "worker"


def visit_threat_page(
    request: VisitRequest,
    *,
    audit: list[dict[str, object]],
    resolve: Resolver = resolve_host,
    runner: Runner | None = None,
    cleanup: Runner | None = None,
    authorize: Authorize | None = None,
) -> ThreatDocument:
    decision = "completed"
    label = source_label(request.url, request.switches)
    owner_id = ""
    try:
        enforce_policy(request)
        digest = intent_digest(request)
        owner_id = _authorize(request, digest, authorize)
        authorize_target(request.url, switches=request.switches, resolve=resolve, allow_hosts=request.allow_hosts)
        capture = _fetch(request, resolve, runner=runner, cleanup=cleanup)
        return ThreatDocument(_sanitize_capture(capture), label, _MARKER)
    except ThreatBrowserError as exc:
        decision = exc.code
        raise
    except Exception:
        decision = "fetch_failed"
        raise
    finally:
        payload = visit_audit(request, decision=decision, source_label=label, owner_id=owner_id)
        append_audit(audit, payload)
        _store_audit(request, payload)


class Authorize:
    def __call__(self, request: VisitRequest, digest: str) -> str: ...


def _store_audit(request: VisitRequest, payload: dict[str, object]) -> None:
    url = os.environ.get("AETHER_WORKER_DATABASE_URL", "")
    if url == "" or request.project_id == "":
        return
    body = dict(payload)
    if request.task_id != "":
        body["task_id"] = request.task_id
    if request.approval_id != "":
        body["approval_id"] = request.approval_id
    try:
        persist_threat_audit(Database(url), project_id=request.project_id, payload=body)
    except Exception as exc:
        raise ThreatBrowserError("audit_failed") from exc


def _authorize(request: VisitRequest, digest: str, authorize: Authorize | None) -> str:
    if os.environ.get("AETHER_PROCESS_ROLE") == _WORKER:
        try:
            return worker_authorize(request.project_id, request.task_id, request.approval_id, digest)
        except GrantError as exc:
            raise ThreatBrowserError(exc.code) from exc
    if authorize is None:
        if request.approval is not None or request.permit is not None:
            raise ThreatBrowserError("verifier_required")
        raise ThreatBrowserError("approval_required")
    try:
        owner_id = authorize(request, digest)
    except ThreatBrowserError:
        raise
    except GrantError as exc:
        raise ThreatBrowserError(exc.code) from exc
    if not isinstance(owner_id, str) or owner_id == "":
        raise ThreatBrowserError("approval_required")
    return owner_id


def _fetch(
    request: VisitRequest,
    resolve: Resolver,
    *,
    runner: Runner | None,
    cleanup: Runner | None,
) -> PageCapture:
    proxy = chosen_proxy(request)
    if request.runtime == "playwright":
        if not fixtures_enabled():
            raise ThreatBrowserError("runtime_rejected")
        return read_page(request.url, switches=request.switches, allow_hosts=request.allow_hosts, resolve=resolve, proxy=proxy)
    if request.runtime != "container":
        raise ThreatBrowserError("runtime_rejected")
    if request.fixture and not fixtures_enabled():
        raise ThreatBrowserError("fixture_hosts_rejected")
    payload = {
        "url": request.url,
        "browser_enabled": request.switches.browser_enabled,
        "tor_enabled": request.switches.tor_enabled,
        "onion_enabled": request.switches.onion_enabled,
        "owner_confirmed": request.switches.owner_confirmed,
        "allow_hosts": sorted(request.allow_hosts or ()),
        "proxy": proxy,
    }
    try:
        if os.environ.get("AETHER_PROCESS_ROLE") == _WORKER:
            raw = remote_execute(
                payload,
                browser_image=request.browser_image,
                tor_image=request.tor_image,
                fixture=request.fixture,
            )
        else:
            if runner is None or cleanup is None:
                raise ThreatBrowserError("runner_required")
            raw = execute_container(
                payload,
                runner=runner,
                cleanup=cleanup,
                browser_image=request.browser_image,
                tor_image=request.tor_image,
                fixture=request.fixture,
            )
    except ThreatLaunchError as exc:
        raise ThreatBrowserError(exc.code) from exc
    return _capture(raw)


def _capture(raw: dict[str, object]) -> PageCapture:
    version = raw.get("playwright_version")
    browser_version = raw.get("browser_version")
    if version != PLAYWRIGHT_VERSION or browser_version != CHROMIUM_BROWSER_VERSION:
        raise ThreatBrowserError("browser_pin_mismatch")
    text = raw.get("text")
    agent = raw.get("user_agent")
    if not isinstance(text, str) or not isinstance(agent, str):
        raise ThreatBrowserError("container_unavailable")
    return PageCapture(text=text, browser_version=str(browser_version), playwright_version=str(version), user_agent=agent)
