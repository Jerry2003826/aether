"""The worker process does not keep the verifier DSN, provider key, or Docker socket."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from aether_worker.startup_budget import wait_worker_ready
from tests.integration.test_worker_process import _start, _stop, _temporal_address

SECRET = "sk-live-should-not-stay"


def test_worker_process_drops_verifier_key_and_docker(role_urls, tmp_path: Path) -> None:
    password = urlsplit(role_urls["aether_verifier"]).password or ""
    assert password
    index_root = tmp_path / "indexes"
    env = _env(role_urls, tmp_path, index_root)
    logs: list[str] = []
    worker = _start([sys.executable, "-m", "aether_worker"], env, logs)
    try:
        wait_worker_ready(logs, worker)
        _assert_process_clean(worker.pid, password)
        for pid in _descendants(worker.pid):
            _assert_process_clean(pid, password)
            assert _docker_fds(pid) == []
        boundary = json.loads((index_root / "boundary.json").read_text(encoding="utf-8"))
        assert boundary["process_role"] == "worker"
        assert boundary["holds_verifier_database_url"] is False
        assert boundary["holds_live_model_key"] is False
        assert boundary["holds_docker_socket"] is False
        verifier = httpx.get(f"{boundary['verifier_base']}/health", timeout=10)
        assert verifier.status_code == 200
        assert verifier.json()["db_user"] == "aether_verifier"
        runner = httpx.get(f"{boundary['runner_base']}/health", timeout=20)
        assert runner.status_code == 200
        assert runner.json()["runtime"] == "enabled"
        assert runner.json()["server_version"]
        text = "".join(logs)
        assert password not in text
        assert SECRET not in text
    finally:
        _stop(worker)


def test_preset_worker_role_refuses_privileged_env(role_urls, tmp_path: Path) -> None:
    password = urlsplit(role_urls["aether_verifier"]).password or ""
    env = _env(role_urls, tmp_path, tmp_path / "indexes")
    env["AETHER_PROCESS_ROLE"] = "worker"
    logs: list[str] = []
    worker = _start([sys.executable, "-m", "aether_worker"], env, logs)
    assert worker.wait(timeout=30) != 0
    text = "".join(logs)
    assert "AETHER_VERIFIER_DATABASE_URL" in text
    assert "AETHER_LIVE_MODEL_KEY" in text
    assert password not in text
    assert SECRET not in text


def _env(role_urls: dict[str, str], tmp_path: Path, index_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AETHER_WORKER_DATABASE_URL": role_urls["aether_worker"],
            "AETHER_VERIFIER_DATABASE_URL": role_urls["aether_verifier"],
            "AETHER_DISPATCHER_DATABASE_URL": role_urls["aether_dispatcher"],
            "AETHER_TEMPORAL_ADDRESS": _temporal_address(),
            "AETHER_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "AETHER_INDEX_ROOT": str(index_root),
            "AETHER_TASK_QUEUE": f"aether-boundary-{uuid.uuid4().hex}",
            "AETHER_RESEARCH_PROVIDER": "scripted",
            "AETHER_LIVE_MODEL_BASE": "http://127.0.0.1:9/v1",
            "AETHER_LIVE_MODEL_ALIAS": "demo",
            "AETHER_LIVE_MODEL_KEY": SECRET,
        }
    )
    env.pop("AETHER_PROCESS_ROLE", None)
    return env


def _assert_process_clean(pid: int, password: str) -> None:
    env = _environ(pid)
    assert "AETHER_VERIFIER_DATABASE_URL" not in env
    assert "AETHER_LIVE_MODEL_KEY" not in env
    blob = "\n".join(env.values())
    assert password not in blob
    assert SECRET not in blob
    assert _docker_fds(pid) == []


def _environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    found: dict[str, str] = {}
    for item in raw:
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        found[key.decode()] = value.decode(errors="replace")
    return found


def _docker_fds(pid: int) -> list[str]:
    root = Path(f"/proc/{pid}/fd")
    found: list[str] = []
    if not root.is_dir():
        return found
    for item in root.iterdir():
        try:
            target = os.readlink(item)
        except OSError:
            continue
        if "docker.sock" in target:
            found.append(target)
    return found


def _descendants(pid: int) -> list[int]:
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        if f"PPid:\t{pid}\n" in text:
            child = int(entry.name)
            found.append(child)
            found.extend(_descendants(child))
    return found
