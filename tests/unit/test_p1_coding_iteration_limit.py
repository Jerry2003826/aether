"""A coding task that needs more than four agent steps still ends FINISHED (live run l31, P0).

The sandbox entry ran the OpenHands conversation with max_iteration_per_run=4 hard-coded, and the
gateway capped a job at 4 model calls: a live job that fixed the code (tests passed) was cut off at
step 5, its finish step ("Agent reached maximum iterations limit (4)"), and judged FAILED as
agent_interrupted. The limit is now AETHER_CODING_MAX_ITERATIONS (default 30, 1..200), read by the
worker, handed to the sandbox entry and used as the gateway's per-job call cap, so the two never
silently differ; total spend stays bounded by the job's money, token and request budget.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_openhands_coding import (  # noqa: F401  (_sandbox_shared_memory is an autouse fixture)
    _ENTRY,
    _PYTHON,
    _SIGN,
    _app_path,
    _completion,
    _sandbox_shared_memory,
    _SocketModel,
)
from tests.unit.test_p1_coding_request_limit import _calls, _open, _setup
from tests.unit.test_p1_proxy_drain import _FENCE, _JOB, _KEY, _PROJECT, _TASK
from tests.unit.test_r10_model_channel_worker import _Job, _revoke_in_band

_ENV = "AETHER_CODING_MAX_ITERATIONS"


class _StepModel(_SocketModel):
    """Five working steps (look, read, edit, check) before the finish call: six model calls."""

    def reply(self, raw: bytes, authorization: str) -> dict:
        self.authorizations.append(authorization)
        self.bodies.append(raw)
        self._calls += 1
        text = raw.decode("utf-8", "replace")
        app = _app_path(text)
        steps = [
            ("terminal", {"command": "ls"}),
            ("terminal", {"command": f"cat {app}"}),
            ("terminal", {"command": "python3 -c 'print(6 * 7)'"}),
            ("file_editor", {"command": "str_replace", "path": app, "old_str": "return 0", "new_str": "return 42"}),
            ("terminal", {"command": f"cat {app}"}),
        ]
        if self._calls <= len(steps):
            name, arguments = steps[self._calls - 1]
            return _completion(name, json.dumps(arguments), f"call_{self._calls}")
        return _completion("finish", json.dumps({"message": "done"}), "call_finish")


def _run_entry(tmp_path: Path, extra: dict[str, str]) -> tuple[dict, _StepModel]:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    fake = _StepModel()
    fake.start()
    protocol = tmp_path / "protocol.py"
    protocol.write_text(
        _SIGN + "import json, sys\n"
        "sys.stdout.write(json.dumps({'nonce': _NONCE, 'passed': 1, 'failed': 0, 'errors': 0, 'cases': []}) + '\\n')\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != _ENV}
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_OBJECTIVE": f"Edit {job / 'app.py'} so value() returns 42.",
            "AETHER_SANDBOX_PROTOCOL": str(protocol),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
            **extra,
        }
    )
    try:
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180, check=False,
        )
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout[-3000:] + completed.stderr[-3000:]
    return json.loads(line[len("RESULT ") :]), fake


def test_a_task_that_needs_six_steps_finishes_under_the_default_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aether_domain.coding_limits import coding_max_iterations

    monkeypatch.delenv(_ENV, raising=False)
    # What container_arguments hands the entry when nothing is configured.
    body, fake = _run_entry(tmp_path, {_ENV: str(coding_max_iterations())})
    assert body["agent_completed"] is True, body
    assert body["model_requests"] == 6 == len(fake.bodies)
    assert "return 42" in body["patch"]
    assert body["passed"] == 1


@pytest.mark.parametrize(("raw", "detail"), [(None, "iteration_limit_missing"), ("0", "iteration_limit_invalid"), ("x", "iteration_limit_invalid")])
def test_the_entry_has_no_limit_of_its_own_and_refuses_a_missing_or_invalid_one(tmp_path: Path, raw: str | None, detail: str) -> None:
    body, fake = _run_entry(tmp_path, {} if raw is None else {_ENV: raw})
    assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": detail}]
    assert fake.bodies == []
    assert body["agent_completed"] is False


def test_the_limit_is_configurable_with_a_realistic_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_domain.coding_limits import CODING_MAX_ITERATIONS, coding_max_iterations

    monkeypatch.delenv(_ENV, raising=False)
    assert CODING_MAX_ITERATIONS == 30
    assert coding_max_iterations() == 30
    monkeypatch.setenv(_ENV, "12")
    assert coding_max_iterations() == 12
    for bad in ("0", "-1", "201", "ten", "1.5"):
        monkeypatch.setenv(_ENV, bad)
        with pytest.raises(ValueError, match=_ENV):
            coding_max_iterations()


def test_the_worker_hands_the_limit_to_the_sandbox_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aether_worker.coding_sandbox import CodingSandboxError, container_arguments

    monkeypatch.delenv(_ENV, raising=False)
    arguments = container_arguments(tmp_path, "image", objective="fix it")
    assert f"{_ENV}=30" in arguments
    assert arguments[arguments.index(f"{_ENV}=30") - 1] == "-e"
    monkeypatch.setenv(_ENV, "12")
    assert f"{_ENV}=12" in container_arguments(tmp_path, "image", objective="fix it")
    monkeypatch.setenv(_ENV, "0")
    with pytest.raises(CodingSandboxError, match="coding_max_iterations_invalid"):
        container_arguments(tmp_path, "image", objective="fix it")


def test_the_gateway_call_cap_follows_the_same_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "6")
    db, gateway = _setup(monkeypatch, max_requests=48)
    for index in range(1, 7):
        _open(gateway, index)
    from aether_gateway.model import GatewayDenied

    with pytest.raises(GatewayDenied) as caught:
        _open(gateway, 7)
    assert caught.value.code == "budget_exceeded"
    assert _calls(db) == 6


def test_the_default_gateway_call_cap_is_not_four(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_gateway.model import coding_max_model_calls

    monkeypatch.delenv(_ENV, raising=False)
    assert coding_max_model_calls() == 30


def test_an_invalid_limit_stops_the_worker_and_the_gateway_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from aether_domain.coding_limits import require_coding_limits

    monkeypatch.setenv(_ENV, "0")
    with pytest.raises(SystemExit, match=_ENV):
        require_coding_limits()
    monkeypatch.setenv(_ENV, "8")
    require_coding_limits()


def _chat(path: Path, token: str, index: int) -> int:
    raw = json.dumps({"model": "coder", "messages": [{"role": "user", "content": f"step {index}"}]}).encode()
    request = (
        "POST /v1/chat/completions HTTP/1.1\r\n"
        f"authorization: Bearer {token}\r\n"
        f"content-length: {len(raw)}\r\n"
        "content-type: application/json\r\nconnection: close\r\n\r\n"
    ).encode() + raw
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(20)
        sock.connect(str(path))
        sock.sendall(request)
        chunks = []
        while block := sock.recv(4096):
            chunks.append(block)
    return int(b"".join(chunks).split(b" ", 2)[1])


class _SixCallJob(_Job):
    """A whole coding job (real proxy, in-memory ledger) whose agent makes six model calls; each call
    is admitted by the gateway's own coding-call ticket, where the per-job call cap lives."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch, revoke=True)
        import aether_gateway.model as gateway_model

        monkeypatch.setattr(gateway_model, "_coding_lease_held", lambda _conn, **_fence: True)
        self.statuses: list[int] = []
        self.calls = 0

    def forward(self, _body: dict) -> dict:
        self.calls += 1
        gateway, _session = self._gateway()
        gateway._open_coding_ticket(
            project_id=_PROJECT,
            task_id=_TASK,
            parent_key=_KEY,
            child_key=f"{_KEY}:call:{self.calls:064x}",
            attempt_id=str(_FENCE),
            alias="coder",
            prompt_sha=f"{self.calls:064x}",
            job_id=_JOB,
            operation_id="op-1",
            fencing=_FENCE,
        )
        return self._answer(120, 30)

    def sandbox(self, _root: Path, *, proxy_dir: Path, job_token: str, **_kwargs: Any) -> dict[str, Any]:
        socket_path = proxy_dir / "model.sock"
        self.statuses = [_chat(socket_path, job_token, index) for index in range(1, 7)]
        assert _revoke_in_band(socket_path, job_token) == 200
        graded = {
            "passed": 1,
            "failed": 0,
            "agent_completed": True,
            "model_requests": 6,
            "patch": "--- a/app.py\n+++ b/app.py\n",
            "cases": [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}],
        }
        return {"exit_code": 0, "result": graded}


def test_a_job_making_more_than_four_model_calls_is_not_refused_and_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    job = _SixCallJob(monkeypatch)
    job.run()
    assert job.statuses == [200] * 6, job.statuses
    assert 409 not in job.statuses
    assert job.published == [("FINISHED", None)]
