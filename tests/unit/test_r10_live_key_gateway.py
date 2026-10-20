"""Without AETHER_LIVE_MODEL_KEY there is no model gateway, and nothing is sent or charged.

Reported by Codex (role_launch.py:100): with only the provider key missing, the gateway still started,
so the worker saw a non-empty gateway base and admitted research; the provider's auth failure then
counted as a sent call and its reservation was charged as unknown usage, and the task could not be
resumed once the key was configured.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

import aether_gateway.__main__ as gateway_main
import pytest
from aether_worker import role_launch

_KEY = "sk-unit-live-key-must-not-leak"


def _stub_launch(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    for name in ("AETHER_WORKER_DATABASE_URL", "AETHER_VERIFIER_DATABASE_URL", "AETHER_DISPATCHER_DATABASE_URL", "AETHER_TEMPORAL_ADDRESS"):
        monkeypatch.setenv(name, f"postgresql://unit/{name.lower()}")
    # Registered with monkeypatch so the launcher's writes are undone after the test.
    for name in ("AETHER_VERIFIER_BASE", "AETHER_GATEWAY_BASE", "AETHER_RUNNER_BASE", "AETHER_LOOPBACK_TOKEN", "AETHER_PROCESS_ROLE"):
        monkeypatch.setenv(name, "unset")
        monkeypatch.delenv(name)
    monkeypatch.setattr(role_launch, "stop_recorded_roles", lambda: None)

    class _Proc:
        pid = 1

        def poll(self) -> None:
            return None

    def opened(module: str, _env: Any, _hide: Any, payload: Any) -> tuple[_Proc, list[str]]:
        events.append(f"open {module}")
        return _Proc(), []

    ports = iter(range(41001, 41010))

    def awaited(_proc: Any, _errors: Any, _hide: Any) -> str:
        return f"http://127.0.0.1:{next(ports)}"

    monkeypatch.setattr(role_launch, "_open", opened)
    monkeypatch.setattr(role_launch, "_await_role", awaited)
    monkeypatch.setattr(role_launch.subprocess, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(role_launch, "_require_health", lambda base, role, _hide: events.append(f"health {role}"))
    for name in ("_record_role_pids", "_write_boundary", "_close_pipes"):
        monkeypatch.setattr(role_launch, name, lambda *_a, **_k: None)


@pytest.mark.parametrize("value", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_without_the_live_key_no_gateway_starts_and_the_worker_sees_no_gateway(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str | None
) -> None:
    events: list[str] = []
    _stub_launch(monkeypatch, events)
    if value is None:
        monkeypatch.delenv("AETHER_LIVE_MODEL_KEY", raising=False)
    else:
        monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", value)
    ports, _children = role_launch.launch_privileged_roles()
    assert "open aether_gateway" not in events, events
    assert "health gateway" not in events, events
    assert ports.gateway_base == ""
    # The worker's admission check (_missing_live_settings) then reports AETHER_LIVE_MODEL_KEY missing.
    assert os.environ.get("AETHER_GATEWAY_BASE", "") == ""
    assert "open aether_worker.verifier_process" in events and "open aether_worker.runner_process" in events
    captured = capsys.readouterr()
    assert "AETHER_LIVE_MODEL_KEY" in captured.err  # an explicit note in the log, never the value


def test_with_the_live_key_the_gateway_starts_and_its_value_is_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    _stub_launch(monkeypatch, events)
    monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", _KEY)
    ports, _children = role_launch.launch_privileged_roles()
    assert "open aether_gateway" in events and "health gateway" in events
    assert ports.gateway_base.startswith("http://127.0.0.1:")
    assert os.environ.get("AETHER_GATEWAY_BASE") == ports.gateway_base
    assert "AETHER_LIVE_MODEL_KEY" not in os.environ
    captured = capsys.readouterr()
    assert _KEY not in captured.out + captured.err


def _gateway_post(monkeypatch: pytest.MonkeyPatch, *, key: str, base: str) -> tuple[Any, list[str]]:
    calls: list[str] = []
    captured: dict[str, Any] = {}

    class _Spy:
        def __init__(self, _database: Any, **_kwargs: Any) -> None:
            pass

        def complete(self, _token: str, _body: dict) -> dict:
            calls.append("complete")
            raise gateway_main.GatewayDenied("upstream_error", sent=True)

        def relay_chat(self, _token: str, _body: dict) -> dict:
            calls.append("relay_chat")
            return {}

    monkeypatch.setattr(gateway_main, "_handoff", lambda: {"provider_key": key, "worker_url": "postgresql://unit/worker"})
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "loopback-unit")
    monkeypatch.setenv("AETHER_LIVE_MODEL_BASE", base)
    monkeypatch.setattr(gateway_main, "Database", lambda _url: object())
    monkeypatch.setattr(gateway_main, "ModelGateway", _Spy)
    monkeypatch.setattr(gateway_main, "serve_role", lambda _token, health, post: captured.update(health=health, post=post))
    gateway_main.main()
    return captured["post"], calls


def test_a_gateway_without_a_key_refuses_research_calls_unsent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defence in depth: a gateway started without a key (an older launcher, a manual start) refuses
    # before any reservation is spent and before the provider is contacted.
    post, calls = _gateway_post(monkeypatch, key="", base="http://127.0.0.1:9/v1")
    status, body = post("/v1/complete", {"token": "call", "alias": "a", "prompt": "p", "max_tokens": 4})
    assert (status, body) == (409, {"denied": True, "code": "live_model_key_missing", "sent": False})
    assert calls == []


def test_a_gateway_with_a_key_still_calls_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    post, calls = _gateway_post(monkeypatch, key=_KEY, base="http://127.0.0.1:9/v1")
    status, body = post("/v1/complete", {"token": "call", "alias": "a", "prompt": "p", "max_tokens": 4})
    assert calls == ["complete"]
    assert (status, body) == (409, {"denied": True, "code": "upstream_error", "sent": True})
