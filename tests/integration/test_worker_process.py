"""Process-level proof that the documented worker consumes public HTTP commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from aether_api.auth_service import issue_bootstrap_secret
from aether_storage.db import Database
from aether_worker.dispatcher import deliver_pending
from tests.integration.test_auth import ORIGIN


PASSPHRASE = "correct horse battery"
ORIGIN_VALUE = "http://127.0.0.1:3000"
ROOT = Path(__file__).resolve().parents[2]


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _temporal_address() -> str:
    address = os.environ.get("AETHER_TEMPORAL_ADDRESS")
    if address:
        return address
    if os.environ.get("AETHER_REQUIRE_SERVICES") == "1":
        pytest.fail("AETHER_TEMPORAL_ADDRESS is required")
    pytest.skip("AETHER_TEMPORAL_ADDRESS unset; workflow not_run")


class _Down:
    async def start_workflow(self, *args, **kwargs):
        raise ConnectionError("temporal unavailable")


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {**ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key}


def test_dispatcher_leaves_console_commands_pending(client, api_db, role_urls) -> None:
    from tests.integration.test_artifacts import _project

    _owner, project_id, headers = _project(client)
    source = client.post(
        f"/v1/projects/{project_id}/sources",
        headers={**headers, "Idempotency-Key": "route-source"},
        json={"kind": "url", "locator": "https://example.test/route", "allowed_domains": ["example.test"]},
    )
    task = client.post(
        f"/v1/projects/{project_id}/tasks",
        headers={**headers, "Idempotency-Key": "route-task"},
        json={"goal": "只投递任务命令"},
    )
    assert source.status_code == 202, source.text
    assert task.status_code == 202, task.text
    dispatcher = Database(role_urls["aether_dispatcher"])
    result = asyncio.run(deliver_pending(dispatcher, _Down(), task_queue="audit-route"))  # type: ignore[arg-type]
    assert task.json()["command_id"] in result["failed"]
    assert source.json()["command_id"] not in result["delivered"]
    assert source.json()["command_id"] not in result["failed"]
    with api_db.transaction(owner_id=_owner, project_id=project_id) as conn:
        rows = {
            row["kind"]: row["status"]
            for row in conn.execute(
                text("SELECT kind, status FROM command_outbox WHERE project_id = :project_id"),
                {"project_id": project_id},
            ).mappings()
        }
    assert rows["source.create"] == "PENDING"
    assert rows["start"] == "DELIVERING"
    listed = client.get(f"/v1/projects/{project_id}/sources")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []


def _drain(pipe, bucket: list[str]) -> None:
    assert pipe is not None
    for line in pipe:
        bucket.append(line)


def _wait_line(bucket: list[str], needle: str, proc: subprocess.Popen[str], timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(needle in line for line in bucket):
            return
        if proc.poll() is not None:
            raise AssertionError(f"进程提前退出 {proc.returncode}: {''.join(bucket[-40:])}")
        time.sleep(0.1)
    raise AssertionError(f"没有看到 {needle}: {''.join(bucket[-40:])}")


def _wait_http(url: str) -> None:
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code < 500:
                return
            last = str(response.status_code)
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.2)
    raise AssertionError(f"服务没有起来: {url} {last}")


def _start(args: list[str], env: dict[str, str], bucket: list[str]) -> subprocess.Popen[str]:
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=_drain, args=(proc.stdout, bucket), daemon=True).start()
    return proc


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_documented_worker_consumes_http_commands_without_duplicate_effects(role_urls, clean_db, tmp_path: Path) -> None:
    address = _temporal_address()
    api_port = _port()
    queue = f"aether-audit-{uuid.uuid4()}"
    artifact_root = tmp_path / "artifacts"
    secret_path = ROOT / "secrets" / f"audit-worker-{uuid.uuid4().hex}.json"
    secret = issue_bootstrap_secret(secret_path)
    env = os.environ.copy()
    env.update(
        {
            "AETHER_DATABASE_URL": role_urls["aether_api"],
            "AETHER_WORKER_DATABASE_URL": role_urls["aether_worker"],
            "AETHER_VERIFIER_DATABASE_URL": role_urls["aether_verifier"],
            "AETHER_DISPATCHER_DATABASE_URL": role_urls["aether_dispatcher"],
            "AETHER_TEMPORAL_ADDRESS": address,
            "AETHER_ARTIFACT_ROOT": str(artifact_root),
            "AETHER_TASK_QUEUE": queue,
            "AETHER_INTAKE_PAUSE_SECONDS": "4",
            "AETHER_BIND_HOST": "127.0.0.1",
            "AETHER_API_PORT": str(api_port),
            "AETHER_BOOTSTRAP_PATH": str(secret_path),
            "AETHER_ORIGINS": ORIGIN_VALUE,
            "AETHER_COOKIE_SECURE": "",
        }
    )
    api_logs: list[str] = []
    worker_logs: list[str] = []
    api = _start([sys.executable, "-m", "aether_api.main"], env, api_logs)
    worker = _start([sys.executable, "-m", "aether_worker"], env, worker_logs)
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        _wait_line(worker_logs, "aether-worker-ready", worker)
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=10) as http:
            bootstrap = http.post(
                "/v1/auth/bootstrap",
                headers={"Origin": ORIGIN_VALUE},
                json={"bootstrap_secret": secret, "display_name": "所有者", "passphrase": PASSPHRASE},
            )
            assert bootstrap.status_code == 201, bootstrap.text + "".join(api_logs[-20:])
            csrf = bootstrap.json()["csrf_token"]
            project = http.post("/v1/projects", headers=_headers(csrf, "proc-project"), json={"name": "进程项目"})
            assert project.status_code == 201, project.text
            project_id = project.json()["id"]
            locator = "https://example.test/process-source"
            source = http.post(
                f"/v1/projects/{project_id}/sources",
                headers=_headers(csrf, "proc-source"),
                json={"kind": "url", "locator": locator, "allowed_domains": ["example.test"]},
            )
            assert source.status_code == 202, source.text
            fact = "事实：水在标准大气压下于一百摄氏度沸腾"
            uploaded = http.post(
                f"/v1/projects/{project_id}/artifacts",
                headers=_headers(csrf, "proc-source-doc"),
                files={"file": ("note.txt", f"{fact}\n".encode(), "text/plain")},
            )
            assert uploaded.status_code == 201, uploaded.text
            goal = "请根据已上传资料回答沸点"
            created = http.post(
                f"/v1/projects/{project_id}/tasks",
                headers=_headers(csrf, "proc-task"),
                json={"goal": goal},
            )
            assert created.status_code == 202, created.text
            task_id = created.json()["resource_id"]
            _wait_status(http, project_id, task_id, "RUNNING", worker, worker_logs)
            _stop(worker)
            worker = None
            interrupted = http.get(f"/v1/projects/{project_id}/tasks/{task_id}")
            assert interrupted.status_code == 200, interrupted.text
            assert interrupted.json()["status"] == "RUNNING", interrupted.text
            worker_logs.clear()
            worker = _start([sys.executable, "-m", "aether_worker"], env, worker_logs)
            _wait_line(worker_logs, "aether-worker-ready", worker)
            finished = _wait_verified(http, project_id, task_id, fact, goal, worker, worker_logs)
            assert finished["status"] == "SUCCEEDED"
            assert finished["research_calls"] == 1
            _stop(worker)
            worker = None
            second = http.post(
                f"/v1/projects/{project_id}/sources",
                headers=_headers(csrf, "proc-source-2"),
                json={"kind": "url", "locator": locator + "/again", "allowed_domains": ["example.test"]},
            )
            assert second.status_code == 202, second.text
            worker_logs.clear()
            worker = _start([sys.executable, "-m", "aether_worker"], env, worker_logs)
            _wait_line(worker_logs, "aether-worker-ready", worker)
            _wait_locator(http, project_id, locator + "/again", worker, worker_logs)
            stable = _wait_verified(http, project_id, task_id, fact, goal, worker, worker_logs)
            assert stable["research_calls"] == 1
            assert stable["verifications"] == finished["verifications"]
            assert stable["answer_sha"] == finished["answer_sha"]
            sources = http.get(f"/v1/projects/{project_id}/sources")
            assert sources.status_code == 200
            locators = [item["locator"] for item in sources.json()["items"]]
            assert locator in locators
            assert locators.count(locator) == 1
            assert locators.count(locator + "/again") == 1
    finally:
        _stop(worker)
        _stop(api)
        secret_path.unlink(missing_ok=True)


def _wait_status(http: httpx.Client, project_id: str, task_id: str, status: str, worker: subprocess.Popen[str] | None, logs: list[str]) -> dict:
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
        if worker is not None and worker.poll() is not None:
            raise AssertionError(f"worker 退出 {worker.returncode}: {''.join(logs[-40:])}")
        task = http.get(f"/v1/projects/{project_id}/tasks/{task_id}")
        if task.status_code == 200 and task.json()["status"] == status:
            return task.json()
        last = task.text
        time.sleep(0.05)
    raise AssertionError(f"任务没有进入 {status}: {last}\n{''.join(logs[-40:])}")


def _wait_verified(http: httpx.Client, project_id: str, task_id: str, fact: str, goal: str, worker: subprocess.Popen[str], logs: list[str]) -> dict[str, object]:
    deadline = time.time() + 45
    last = ""
    while time.time() < deadline:
        if worker.poll() is not None:
            raise AssertionError(f"worker 退出 {worker.returncode}: {''.join(logs[-40:])}")
        task = http.get(f"/v1/projects/{project_id}/tasks/{task_id}")
        events = http.get(f"/v1/projects/{project_id}/tasks/{task_id}/events")
        if task.status_code != 200:
            last = task.text
            time.sleep(0.2)
            continue
        body = task.json()
        if body["status"] not in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW"}:
            last = task.text
            time.sleep(0.2)
            continue
        passed = next((item for item in body["verification_results"] if item["verdict"] == "PASS"), None)
        assert body["status"] == "SUCCEEDED", task.text
        assert passed is not None, task.text
        assert passed["verifier_identity"] == "aether_verifier"
        answer = b""
        for artifact_id in body["artifact_ids"]:
            downloaded = http.get(f"/v1/projects/{project_id}/artifacts/{artifact_id}")
            assert downloaded.status_code == 200, downloaded.text
            if hashlib.sha256(downloaded.content).hexdigest() == passed["artifact_sha256"]:
                answer = downloaded.content
        assert answer, task.text
        text = answer.decode()
        assert fact in text
        assert text.strip() != goal
        assert json.loads(json.dumps({"goal": goal}))["goal"] != text.strip()
        assert events.text.count("research.call") == 1
        return {"status": body["status"], "verifications": len(body["verification_results"]), "research_calls": 1, "answer_sha": passed["artifact_sha256"]}
    raise AssertionError(f"任务没有完成验证: {last}\n{''.join(logs[-40:])}")


def _wait_locator(http: httpx.Client, project_id: str, locator: str, worker: subprocess.Popen[str], logs: list[str]) -> None:
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
        if worker.poll() is not None:
            raise AssertionError(f"worker 退出 {worker.returncode}: {''.join(logs[-40:])}")
        sources = http.get(f"/v1/projects/{project_id}/sources")
        assert sources.status_code == 200, sources.text
        if any(item["locator"] == locator for item in sources.json()["items"]):
            return
        last = sources.text
        time.sleep(0.2)
    raise AssertionError(f"来源没有被消费: {last}\n{''.join(logs[-40:])}")
