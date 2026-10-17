"""The gateway health report says model_unavailable when it has no provider key or no upstream."""

from __future__ import annotations

from typing import Any, Callable

import pytest

import aether_gateway.__main__ as gateway_main


def _health(monkeypatch: pytest.MonkeyPatch, *, key: str, base: str) -> dict[str, object]:
    captured: dict[str, Callable[..., Any]] = {}
    monkeypatch.setattr(gateway_main, "_handoff", lambda: {"provider_key": key, "worker_url": "postgresql://unit/none"})
    monkeypatch.setattr(gateway_main, "Database", lambda _url: object())
    monkeypatch.setattr(gateway_main, "serve_role", lambda _token, health, post: captured.update(health=health, post=post))
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-unit")
    if base:
        monkeypatch.setenv("AETHER_LIVE_MODEL_BASE", base)
    else:
        monkeypatch.delenv("AETHER_LIVE_MODEL_BASE", raising=False)
    gateway_main.main()
    return captured["health"]()


def test_gateway_without_a_model_key_reports_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _health(monkeypatch, key="", base="http://127.0.0.1:9/v1")
    assert body["role"] == "gateway"
    assert body["configured"] is False
    assert body["status"] == "unavailable"
    assert body["error"] == "model_unavailable"


def test_gateway_without_an_upstream_reports_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _health(monkeypatch, key="unit-key", base="")
    assert body["configured"] is False
    assert body["status"] == "unavailable"
    assert body["error"] == "model_unavailable"


def test_gateway_with_key_and_upstream_reports_available_without_leaking_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _health(monkeypatch, key="unit-secret-key", base="http://127.0.0.1:9/v1")
    assert body["role"] == "gateway"
    assert body["configured"] is True
    assert body["status"] == "available"
    assert "error" not in body
    assert "unit-secret-key" not in repr(body)


def test_coding_relay_without_a_model_key_is_model_unavailable_before_any_send(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_gateway.model import GatewayDenied, ModelGateway

    gateway = ModelGateway(object(), upstream_base="http://127.0.0.1:9/v1", provider_key="")  # type: ignore[arg-type]
    blocked: list[str] = []
    gateway._scope = lambda _token: {  # type: ignore[method-assign]
        "id": "1",
        "project_id": "p",
        "task_id": "t",
        "scope": {"request_key": "coding-1", "attempt_id": "1", "capabilities": ["coding:model"], "aliases": ["coder"]},
    }
    gateway._note_admission_block = lambda _session, code: blocked.append(code)  # type: ignore[method-assign]
    gateway._spend_coding_ticket = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ticket spent"))  # type: ignore[method-assign]
    gateway._post_upstream = lambda _body: (_ for _ in ()).throw(AssertionError("sent upstream"))  # type: ignore[method-assign]
    with pytest.raises(GatewayDenied) as caught:
        gateway.relay_chat("job", {"model": "coder", "messages": [{"role": "user", "content": "hi"}]})
    assert caught.value.code == "model_unavailable"
    assert caught.value.sent is False
    assert blocked == ["model_unavailable"]
