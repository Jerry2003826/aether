import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from reference.models import ArtifactRef, CodingJobRequest
from aether_worker.runner import (
    LinuxRunner,
    _docker,
    _job_label,
    call_gateway,
    docker_available,
    job_environment,
    openhands_launch_plan,
    probe_linux_isolation,
    release_containers,
)

PROVIDER_KEY = "provider-secret-should-not-leak"


def _request(intent: str = "intent-a") -> CodingJobRequest:
    return CodingJobRequest(
        project_id="project-a",
        task_id="task-a",
        intent_id=intent,
        base_artifact=ArtifactRef(
            artifact_id="artifact-base",
            project_id="project-a",
            sha256="a" * 64,
            byte_size=1,
            media_type="text/plain",
        ),
        objective="修改说明",
        contract_sha256="b" * 64,
        allowed_tools=["tool.read"],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_linux_namespace_probe_is_measured_or_not_claimed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AETHER_PROVIDER_KEY", PROVIDER_KEY)
    report = probe_linux_isolation(tmp_path / "host-only")
    if report["status"] == "not_supported":
        return
    assert report["status"] == "probed"
    assert report["network_errno"] == 101
    assert report["pid"] == 1
    assert report["host_marker_hidden"] is True
    assert report["provider_key_present"] is False
    (tmp_path / "platform.json").write_text(json.dumps(report), encoding="utf-8")


def test_start_is_idempotent_and_restart_attaches_without_the_provider_key(tmp_path) -> None:
    runner = LinuxRunner(tmp_path / "state")
    request = _request()
    token = "job-token-only"
    key = f"{request.project_id}:{request.task_id}:{request.intent_id}"
    try:
        first = runner.start(request, token=token, gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        second = runner.start(request, token=token, gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        assert first.job_id == second.job_id
        restored = LinuxRunner(tmp_path / "state")
        attached = restored.get(request.project_id, first.job_id)
        assert attached.job_id == first.job_id
        assert attached.status == "ACTIVE"
        saved = (tmp_path / "state" / "jobs.json").read_text(encoding="utf-8")
        assert json.loads(saved)[key]["spawn_count"] == 1
        assert PROVIDER_KEY not in saved
        plan = openhands_launch_plan(token=token, gateway_url="http://127.0.0.1:9/v1/chat")
        assert plan["docker_socket"] is False
        assert plan["mounts"] == []
        assert PROVIDER_KEY not in json.dumps(plan)
        cancelled = restored.cancel(request.project_id, first.job_id)
        assert cancelled.status == "CANCELLED"
        assert json.loads((tmp_path / "state" / "jobs.json").read_text(encoding="utf-8"))[key]["external_effect"] == "unknown"
    finally:
        release_containers(key)


def test_openhands_sdk_points_at_the_gateway_without_the_provider_key() -> None:
    root = os.getcwd()
    python = os.path.join(root, "environments", "openhands", ".venv", "bin", "python")
    if not os.path.exists(python) or not os.path.exists(os.path.join(root, "environments", "openhands", "uv.lock")):
        report = {"status": "not_supported", "reason": "openhands_venv_missing"}
        assert report["status"] == "not_supported"
        return
    env = os.environ.copy()
    env["AETHER_PROVIDER_KEY"] = PROVIDER_KEY
    completed = subprocess.run(
        [python, "probe_import.py"],
        cwd=os.path.join(root, "environments", "openhands"),
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["llm_base_url"] == "http://127.0.0.1:9/v1"
    assert PROVIDER_KEY not in completed.stdout
    plan = openhands_launch_plan(token="job-token-only", gateway_url=payload["llm_base_url"])
    assert plan["dependency_install"] == "prebuilt_only"


def test_job_call_uses_the_gateway_token(tmp_path) -> None:
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            seen["authorization"] = self.headers.get("authorization")
            seen["body"] = raw.decode()
            data = b'{"output":"ok"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = job_environment(token="job-token-only", gateway_url=f"http://127.0.0.1:{server.server_address[1]}/")
    root = os.getcwd()
    env["PYTHONPATH"] = ":".join(
        [root, f"{root}/services/worker", f"{root}/packages/domain", f"{root}/packages/storage", f"{root}/services/api", f"{root}/services/gateway"]
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "import json,os; from aether_worker.runner import call_gateway; print(json.dumps(call_gateway(dict(os.environ), prompt='ping')))"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        server.shutdown()
    assert completed.returncode == 0, completed.stderr
    assert PROVIDER_KEY not in completed.stdout
    assert seen["authorization"] == "Bearer job-token-only"
    assert PROVIDER_KEY not in seen["body"]
    assert "ping" in seen["body"]


def test_container_is_single_isolated_and_reattaches(tmp_path, monkeypatch) -> None:
    if not docker_available():
        pytest.fail("Docker is required to prove container isolation")
    monkeypatch.setenv("AETHER_PROVIDER_KEY", PROVIDER_KEY)
    monkeypatch.setenv("AETHER_ADMIN_DATABASE_URL", "postgresql://postgres:admin-secret@127.0.0.1/postgres")
    request = _request("intent-container")
    key = f"{request.project_id}:{request.task_id}:{request.intent_id}"
    token = "job-token-only"
    try:
        runner = LinuxRunner(tmp_path / "state")
        first = runner.start(request, token=token, gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        second = runner.start(request, token=token, gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        assert first.job_id == second.job_id
        assert _job_count(key) == 1
        restarted = LinuxRunner(tmp_path / "state")
        attached = restarted.start(request, token=token, gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        blank = LinuxRunner(tmp_path / "restarted")
        reattached = blank.start(request, token=token, gateway_url="http://127.0.0.1:9/v1/chat", provider_key=PROVIDER_KEY)
        assert attached.job_id == first.job_id
        assert reattached.job_id == first.job_id
        assert _job_count(key) == 1
        report = _container_report(key)
        assert report["internet_blocked"] is True
        assert report["metadata_blocked"] is True
        assert report["docker_socket"] is False
        assert report["forbidden"] == []
        assert report["gateway"]["usage_status"] == "scripted"
        assert report["gateway"]["output"] == "scripted"
        encoded = json.dumps(report)
        assert PROVIDER_KEY not in encoded
        assert "admin-secret" not in encoded
    finally:
        release_containers(key)


def _job_count(key: str) -> int:
    label = _job_label(key)
    completed = _docker(["ps", "-aq", "--filter", f"label=aether.job={label}", "--filter", "label=aether.role=job"])
    return len([line for line in completed.stdout.splitlines() if line.strip()])


def _container_report(key: str) -> dict:
    label = _job_label(key)
    completed = _docker(["ps", "-q", "--filter", f"label=aether.job={label}", "--filter", "label=aether.role=job"])
    container_id = completed.stdout.strip()
    probe = (
        "import json,os,socket,urllib.request\n"
        "markers=('PROVIDER','ADMIN','SECRET','DATABASE','POSTGRES','PASSWORD')\n"
        "forbidden=[key for key in os.environ if any(item in key.upper() for item in markers)]\n"
        "def blocked(host, port):\n"
        "    sock=socket.socket(); sock.settimeout(2)\n"
        "    try:\n"
        "        sock.connect((host, int(port)))\n"
        "    except OSError:\n"
        "        return True\n"
        "    return False\n"
        "req=urllib.request.Request(os.environ['AETHER_GATEWAY_URL'], data=b'{\"alias\":\"coder\",\"prompt\":\"ping\"}', headers={'authorization':'Bearer '+os.environ['AETHER_JOB_TOKEN'],'content-type':'application/json'}, method='POST')\n"
        "with urllib.request.urlopen(req, timeout=5) as response:\n"
        "    gateway=json.loads(response.read().decode())\n"
        "print(json.dumps({'forbidden':forbidden,'docker_socket':os.path.exists('/var/run/docker.sock'),'internet_blocked':blocked(os.environ['AETHER_PROBE_INTERNET'], os.environ['AETHER_PROBE_INTERNET_PORT']),'metadata_blocked':blocked(os.environ['AETHER_PROBE_METADATA'], os.environ['AETHER_PROBE_METADATA_PORT']),'gateway':gateway}))\n"
    )
    # Public DNS and cloud metadata addresses are the probes, not destinations we contact from the host.
    internet = "1.1.1.1"  # NOSONAR
    metadata = "169.254.169.254"  # NOSONAR
    result = _docker(
        [
            "exec",
            "-e",
            f"AETHER_PROBE_INTERNET={internet}",
            "-e",
            "AETHER_PROBE_INTERNET_PORT=443",
            "-e",
            f"AETHER_PROBE_METADATA={metadata}",
            "-e",
            "AETHER_PROBE_METADATA_PORT=80",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            container_id,
            "python",
            "-c",
            probe,
        ]
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])
