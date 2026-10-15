"""Run one research task through the public API and worker against the live provider.

The provider key stays in the environment. It is not printed and not written to the result file.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "packages" / "storage"))
sys.path.insert(0, str(ROOT / "packages" / "domain"))

from aether_api.auth_service import issue_bootstrap_secret
from aether_worker.live_harness import LIVE_ALIAS, LIVE_BASE, LIVE_KEY, redact_secret

ALLOWED_OUT = Path("reports/live_task_check.json")
RESULT_PATH = ROOT / ALLOWED_OUT
FACT = "事实：水在标准大气压下于一百摄氏度沸腾"
GOAL = "请根据已上传资料回答沸点。回答时请引用资料原句。"
ORIGIN = "http://127.0.0.1:3000"
PASSPHRASE = secrets.token_urlsafe(18)
REQUIRED = (
    "AETHER_DATABASE_URL",
    "AETHER_WORKER_DATABASE_URL",
    "AETHER_VERIFIER_DATABASE_URL",
    "AETHER_DISPATCHER_DATABASE_URL",
    "AETHER_TEMPORAL_ADDRESS",
    LIVE_BASE,
    LIVE_ALIAS,
    LIVE_KEY,
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _missing() -> list[str]:
    return [name for name in REQUIRED if not os.environ.get(name)]


def _report(status: str, **fields: object) -> dict[str, object]:
    body: dict[str, object] = {"status": status, "live_ready": False, "provider": "live"}
    body.update(fields)
    return body


def _write(report: dict[str, object]) -> str:
    secret = os.environ.get(LIVE_KEY) or ""
    text = redact_secret(json.dumps(report, ensure_ascii=False, indent=2) + "\n", secret)
    if secret and secret in text:
        text = text.replace(secret, "[REDACTED]")
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(text, encoding="utf-8")
    return text


def _start(args: list[str], env: dict[str, str]) -> tuple[subprocess.Popen[str], Path]:
    descriptor, name = tempfile.mkstemp(prefix="aether-live-")
    os.close(descriptor)
    log_path = Path(name)
    handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, text=True)
    handle.close()
    return proc, log_path


def _read_log(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _wait_ready(api_url: str, api: subprocess.Popen[str], worker_log: Path, worker: subprocess.Popen[str]) -> bool:
    # A fresh runner can spend most of a minute importing the worker.
    deadline = time.time() + 90
    http_ok = False
    while time.time() < deadline:
        if not http_ok:
            try:
                if httpx.get(api_url, timeout=2).status_code < 500:
                    http_ok = True
            except httpx.HTTPError:
                pass
        worker_ok = "aether-worker-ready" in _read_log(worker_log)
        if http_ok and worker_ok:
            return True
        if (api.poll() is not None and not http_ok) or (worker.poll() is not None and not worker_ok):
            return False
        time.sleep(0.2)
    return False


def _log_tail(path: Path | None, secret: str) -> str:
    text = redact_secret(_read_log(path), secret)
    if len(text) > 4000:
        text = text[-4000:]
    return text


def _exception_report(step: str, exc: BaseException) -> dict[str, object]:
    secret = os.environ.get(LIVE_KEY) or ""
    return _report(
        "failed",
        reason="exception",
        step=step,
        error_type=type(exc).__name__,
        error_message=redact_secret(str(exc), secret),
    )


def _run_task(env: dict[str, str]) -> dict[str, object]:
    step = "startup"
    api_port = _port()
    queue = f"aether-live-check-{uuid.uuid4()}"
    artifact_root = ROOT / "data" / "live-task-check"
    artifact_root.mkdir(parents=True, exist_ok=True)
    secret_path = ROOT / "secrets" / f"live-task-{uuid.uuid4().hex}.json"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = issue_bootstrap_secret(secret_path)
    child = os.environ.copy()
    child.update(env)
    child.update(
        {
            "AETHER_RESEARCH_PROVIDER": "live",
            "AETHER_BIND_HOST": "127.0.0.1",
            "AETHER_API_PORT": str(api_port),
            "AETHER_TASK_QUEUE": queue,
            "AETHER_ARTIFACT_ROOT": str(artifact_root),
            "AETHER_BOOTSTRAP_PATH": str(secret_path),
            "AETHER_ORIGINS": ORIGIN,
            "AETHER_COOKIE_SECURE": "",
            "AETHER_INTAKE_PAUSE_SECONDS": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    step = "start_processes"
    api: subprocess.Popen[str] | None = None
    worker: subprocess.Popen[str] | None = None
    api_log: Path | None = None
    worker_log: Path | None = None
    try:
        api, api_log = _start([sys.executable, "-m", "aether_api.main"], child)
        worker, worker_log = _start([sys.executable, "-m", "aether_worker"], child)
        step = "wait_ready"
        if not _wait_ready(f"http://127.0.0.1:{api_port}/health/live", api, worker_log, worker):
            time.sleep(0.2)
            secret_value = os.environ.get(LIVE_KEY) or ""
            return _report(
                "incomplete",
                reason="api_or_worker_did_not_start",
                step=step,
                api_returncode=None if api.poll() is None else api.returncode,
                worker_returncode=None if worker.poll() is None else worker.returncode,
                api_log=_log_tail(api_log, secret_value),
                worker_log=_log_tail(worker_log, secret_value),
            )
        with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=30) as http:
            step = "bootstrap"
            bootstrap = http.post(
                "/v1/auth/bootstrap",
                headers={"Origin": ORIGIN},
                json={"bootstrap_secret": secret, "display_name": "所有者", "passphrase": PASSPHRASE},
            )
            if bootstrap.status_code != 201:
                return _report("failed", reason="bootstrap_failed", step=step, http_status=bootstrap.status_code)
            csrf = bootstrap.json()["csrf_token"]

            def headers(key: str) -> dict[str, str]:
                return {"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key}

            step = "create_project"
            project = http.post("/v1/projects", headers=headers("live-project"), json={"name": "真实模型检查"})
            if project.status_code != 201:
                return _report("failed", reason="project_failed", step=step, http_status=project.status_code)
            project_id = project.json()["id"]
            step = "upload"
            uploaded = http.post(
                f"/v1/projects/{project_id}/artifacts",
                headers=headers("live-doc"),
                files={"file": ("note.txt", f"{FACT}\n".encode(), "text/plain")},
            )
            if uploaded.status_code != 201:
                return _report("failed", reason="upload_failed", step=step, http_status=uploaded.status_code)
            step = "read_upload"
            artifact_id = str(uploaded.json()["artifact_id"])
            step = "create_task"
            created = http.post(
                f"/v1/projects/{project_id}/tasks",
                headers=headers("live-task"),
                json={"goal": GOAL, "allowed_source_ids": [artifact_id]},
            )
            if created.status_code != 202:
                return _report("failed", reason="task_failed", step=step, http_status=created.status_code)
            task_id = created.json()["resource_id"]
            step = "poll_task"
            deadline = time.time() + 120
            last: dict[str, object] = {}
            while time.time() < deadline:
                task = http.get(f"/v1/projects/{project_id}/tasks/{task_id}")
                if task.status_code == 200:
                    last = task.json()
                    if last.get("status") in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW", "CANCELLED"}:
                        break
                time.sleep(0.5)
            step = "read_events"
            events = http.get(f"/v1/projects/{project_id}/tasks/{task_id}/events")
            event_text = events.text if events.status_code == 200 else ""
            call_count = event_text.count("research.call")
            if last.get("status") not in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW", "CANCELLED"} or call_count != 1:
                return _report("failed", task_status=last.get("status"), research_calls=call_count, reason="task_did_not_finish_one_live_call", step=step)
            return _report(
                "probed",
                task_status=last.get("status"),
                requested_alias=os.environ.get(LIVE_ALIAS),
                research_calls=call_count,
                verification_count=len(last.get("verification_results") or []),
                cost_status="unknown",
            )
    except Exception as exc:
        return _exception_report(step, exc)
    finally:
        _stop(worker)
        _stop(api)
        secret_path.unlink(missing_ok=True)
        for path in (api_log, worker_log):
            if path is not None:
                path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if Path(args.out) != ALLOWED_OUT:
        raise SystemExit("out must be reports/live_task_check.json")
    missing = _missing()
    if missing:
        text = _write(_report("blocked", missing=missing, research_calls=0))
        sys.stdout.write(text)
        return 2
    try:
        report = _run_task(dict(os.environ))
    except Exception as exc:
        report = _exception_report(str(getattr(exc, "step", "run_task")), exc)
    text = _write(report)
    sys.stdout.write(text)
    return {"probed": 0, "failed": 1, "blocked": 2, "incomplete": 3}[str(report["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
