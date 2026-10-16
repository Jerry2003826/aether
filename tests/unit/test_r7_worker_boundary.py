"""Worker process role drops verifier and provider credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import aether_worker.goal_effect as goal_effect
from aether_worker.goal_effect import WorkerEndpoints, _missing_live_settings
from aether_worker.loopback import post_json
from aether_worker.role_http import RoleHTTPServer
from aether_worker.role_launch import _record_role_pids, reject_privileged_worker_env, scrub_privileged_env
from aether_worker.service import _forward_verifier


def _endpoints(**overrides: str) -> WorkerEndpoints:
    values = {
        "worker_url": "postgresql://worker",
        "verifier_url": "",
        "dispatcher_url": "postgresql://dispatcher",
        "artifact_root": "/tmp/artifacts",
        "temporal_address": "127.0.0.1:7233",
        "task_queue": "aether-tasks",
        "live_model_base": "http://127.0.0.1:9/v1",
        "live_model_alias": "demo",
        "live_model_key": "",
        "gateway_base": "",
    }
    values.update(overrides)
    root = Path(str(values.pop("artifact_root")))
    return WorkerEndpoints(artifact_root=root, **values)


def test_scrub_removes_verifier_url_and_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_VERIFIER_DATABASE_URL", "postgresql://aether_verifier:secret@127.0.0.1/aether")
    monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", "sk-live-secret")
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "entry")
    monkeypatch.delenv("AETHER_PROCESS_ROLE")
    scrub_privileged_env()
    assert "AETHER_VERIFIER_DATABASE_URL" not in os.environ
    assert "AETHER_LIVE_MODEL_KEY" not in os.environ
    assert os.environ["AETHER_PROCESS_ROLE"] == "worker"


def test_worker_role_refuses_privileged_env_without_printing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    monkeypatch.setenv("AETHER_VERIFIER_DATABASE_URL", "postgresql://aether_verifier:super-secret-password@127.0.0.1/aether")
    monkeypatch.setenv("AETHER_LIVE_MODEL_KEY", "sk-live-secret")
    with pytest.raises(SystemExit) as raised:
        reject_privileged_worker_env()
    message = str(raised.value)
    assert "AETHER_VERIFIER_DATABASE_URL" in message
    assert "AETHER_LIVE_MODEL_KEY" in message
    assert "super-secret-password" not in message
    assert "sk-live-secret" not in message


def test_forward_verifier_reads_endpoints_after_configure(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(url: str, token: str, payload: dict, *, timeout: float) -> dict:
        seen["url"] = url
        seen["token"] = token
        seen["payload"] = payload
        return {}

    monkeypatch.setattr(goal_effect, "ENDPOINTS", None)
    monkeypatch.setattr("aether_worker.service.post_json", fake)
    goal_effect.configure(_endpoints(verifier_base="http://127.0.0.1:9", loopback_token="token-1"))
    _forward_verifier("owner-1", "project-1")
    assert seen["url"] == "http://127.0.0.1:9/v1/commands"
    assert seen["token"] == "token-1"
    assert seen["payload"] == {"owner_id": "owner-1", "project_id": "project-1"}


def test_loopback_post_ignores_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    server = RoleHTTPServer("loop-token", lambda: {"role": "verifier"}, lambda path, body: (200, {"path": path, "seen": body.get("prompt")}))
    thread = threading.Thread(target=server.serve_until_stopped, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        body = post_json(f"http://127.0.0.1:{port}/v1/complete", "loop-token", {"prompt": "secret-prompt"}, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert body["path"] == "/v1/complete"
    assert body["seen"] == "secret-prompt"


def test_recorded_role_pids_are_reaped_before_the_file_is_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_INDEX_ROOT", str(tmp_path))
    held = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "aether_worker.verifier_process"])
    innocent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "role-pids.json").write_text(json.dumps([held.pid, innocent.pid, os.getpid(), 1, "nope"]), encoding="utf-8")
        _record_role_pids([os.getpid()])
        held.wait(timeout=2)
        assert innocent.poll() is None
        assert json.loads((tmp_path / "role-pids.json").read_text(encoding="utf-8")) == [os.getpid()]
    finally:
        if held.poll() is None:
            held.kill()
        if innocent.poll() is None:
            innocent.kill()


def test_gateway_base_counts_as_configured_model_access() -> None:
    configured = _endpoints(gateway_base="http://127.0.0.1:9")
    assert _missing_live_settings(configured) == []
    missing = _endpoints()
    assert missing.gateway_base == ""
    assert "AETHER_LIVE_MODEL_KEY" in _missing_live_settings(missing)
