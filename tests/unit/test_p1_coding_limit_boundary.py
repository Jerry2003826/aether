"""At exactly the limit the sandbox entry stops first; the gateway never refuses a call (ADR-F47).

The entry's max_iteration_per_run and the gateway's per-job coding-call cap are the same number N
(AETHER_CODING_MAX_ITERATIONS). The OpenHands SDK makes one model call per step and checks the limit
only after a step, so it never starts step N+1: a finish due at step N+1 is never sent (the run ends
with MaxIterationsReached), and a finish made at step N is admitted. Here the real entry runs against
the real coding proxy, and every forwarded call is admitted by the gateway's own coding-call ticket.

With AETHER_CODING_MAX_ITERATIONS=3 the gateway answers exactly three calls, all 200 and none 409, and
the job is FINISHED with agent_stop=iteration_limit. That holds only while the entry's LLM makes no
retries of its own (num_retries=0) and no condenser adds summarising calls (condenser=None): either
would spend the gateway's cap without an agent step. Both are read off the objects the entry builds.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from aether_gateway.model import GatewayDenied
from aether_worker import coding_jobs

from tests.unit.test_openhands_coding import (
    _ENTRY,
    _PYTHON,
    _SIGN,
    _app_path,
    _completion,
    _sandbox_shared_memory,  # noqa: F401  (autouse fixture)
)
from tests.unit.test_p1_coding_iteration_limit import _ENV
from tests.unit.test_p1_proxy_drain import _FENCE, _JOB, _KEY, _PROJECT, _TASK
from tests.unit.test_r10_model_channel_worker import _Job

_LIMIT = 3  # AETHER_CODING_MAX_ITERATIONS: three working steps (the edit is step 2); the finish would be step 4


def _scripted(index: int, text: str) -> dict:
    app = _app_path(text)
    steps = [
        ("terminal", {"command": f"cat {app}"}),
        ("file_editor", {"command": "str_replace", "path": app, "old_str": "return 0", "new_str": "return 42"}),
        ("terminal", {"command": f"cat {app}"}),
    ]
    if index <= len(steps):
        name, arguments = steps[index - 1]
        return _completion(name, json.dumps(arguments), f"call_{index}")
    return _completion("finish", json.dumps({"message": "done"}), "call_finish")


class _EntryJob(_Job):
    """A whole coding job: _run_one, the real proxy and ledger admission, and the real sandbox entry."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        super().__init__(monkeypatch, revoke=False)
        import aether_gateway.model as gateway_model

        monkeypatch.setattr(gateway_model, "_coding_lease_held", lambda _conn, **_fence: True)
        self.tmp_path = tmp_path
        self.calls = 0
        self.denied: list[str] = []
        # What the gateway answered each forwarded call: 200, or 409 for a refused ticket (budget_exceeded).
        self.statuses: list[int] = []
        self.built: dict[str, Any] = {}
        self.stored: list[dict] = []
        job = self
        monkeypatch.setattr(
            coding_jobs,
            "_publish",
            lambda *_a, **k: job.published.append((k["state"], k["error_code"])) or job.stored.append(dict(k["sandbox"]["result"])) or True,
        )

    def forward(self, body: dict) -> dict:
        self.calls += 1
        gateway, session = self._gateway()
        try:
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
        except GatewayDenied as denied:
            self.denied.append(denied.code)
            self.statuses.append(409 if denied.code == "budget_exceeded" else 500)
            raise
        answer = _scripted(self.calls, json.dumps(body))
        gateway._add_measured_usage(session, answer)
        self.statuses.append(200)
        return answer

    def sandbox(self, _root: Path, *, proxy_dir: Path, job_token: str, **_kwargs: Any) -> dict[str, Any]:
        protocol = self.tmp_path / "protocol.py"
        protocol.write_text(
            _SIGN + "import json, sys\n"
            "sys.stdout.write(json.dumps({'nonce': _NONCE, 'passed': 1, 'failed': 0, 'errors': 0, 'cases': []}) + '\\n')\n",
            encoding="utf-8",
        )
        work = self.tmp_path / "work"
        job = self.tmp_path / "job"
        work.mkdir()
        (work / "app.py").write_text("def value():\n    return 0\n", encoding="utf-8")
        # Like the container, which gets only the -e list: no credential-named variables from this host.
        markers = ("LIVE_MODEL", "ADMIN_DATABASE", "PASSWORD", "SECRET")
        env = {name: value for name, value in os.environ.items() if not any(part in name for part in markers)}
        env.update(
            {
                "OPENHANDS_SUPPRESS_BANNER": "1",
                "AETHER_SANDBOX_WORK": str(work),
                "AETHER_SANDBOX_JOB": str(job),
                "AETHER_OBJECTIVE": f"Edit {job / 'app.py'} so value() returns 42.",
                "AETHER_SANDBOX_PROTOCOL": str(protocol),
                "AETHER_MODEL_SOCKET": str(proxy_dir / "model.sock"),
                "AETHER_JOB_TOKEN": job_token,
                "AETHER_MODEL_ALIAS": "openai/coder",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        # The real entry and its main(); only the network probe answers as under the container's
        # --network none (errno 101), which this host process cannot reproduce portably. The LLM, Agent
        # and Conversation it builds are recorded as built, so their retry, condenser and limit are checked.
        built = self.tmp_path / "built.json"
        code = (
            f"import json, sys\nsys.path.append({str(_ENTRY.resolve().parent)!r})\n"
            "import openhands_sandbox_entry as entry\nentry.network_errno = lambda: 101\n"
            f"BUILT = {{}}\nPATH = {str(built)!r}\n"
            "def _keep():\n    open(PATH, 'w').write(json.dumps(BUILT))\n"
            "_llm, _agent, _conversation = entry.LLM, entry.Agent, entry.Conversation\n"
            "def LLM(*a, **k):\n    made = _llm(*a, **k)\n    BUILT['num_retries'] = made.num_retries\n    _keep()\n    return made\n"
            "def Agent(*a, **k):\n    made = _agent(*a, **k)\n    BUILT['condenser'] = repr(made.condenser)\n    _keep()\n    return made\n"
            "def Conversation(*a, **k):\n    BUILT['max_iteration_per_run'] = k.get('max_iteration_per_run')\n    _keep()\n"
            "    return _conversation(*a, **k)\n"
            "entry.LLM, entry.Agent, entry.Conversation = LLM, Agent, Conversation\nentry.main()\n"
        )
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", "-c", code],
            cwd=self.tmp_path, env=env, capture_output=True, text=True, timeout=180, check=False,
        )
        self.built = json.loads(built.read_text(encoding="utf-8")) if built.is_file() else {}
        line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
        assert line, completed.stdout[-3000:] + completed.stderr[-3000:]
        return {"exit_code": completed.returncode, "result": json.loads(line[len("RESULT ") :])}


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: int) -> _EntryJob:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    # One setting for both sides: the entry reads it from its environment, the gateway in-process.
    monkeypatch.setenv(_ENV, str(limit))
    job = _EntryJob(monkeypatch, tmp_path)
    job.run()
    return job


def test_at_the_limit_the_entry_stops_first_and_the_gateway_never_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    job = _run(monkeypatch, tmp_path, _LIMIT)
    # No retries and no condenser: every gateway call is one agent step.
    assert job.built == {"num_retries": 0, "condenser": "None", "max_iteration_per_run": _LIMIT}
    # Exactly N calls, all answered 200; the finish (call N+1) is never attempted, so no 409/budget_exceeded.
    assert job.calls == _LIMIT
    assert job.statuses == [200, 200, 200]
    assert job.denied == []
    assert job.published == [("FINISHED", None)]
    assert job.stored[0]["agent_stop"] == "iteration_limit"
    assert job.stored[0]["agent_completed"] is False
    assert "return 42" in job.stored[0]["patch"]


def test_a_finish_made_at_exactly_the_limit_is_admitted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    job = _run(monkeypatch, tmp_path, _LIMIT + 1)
    assert job.built == {"num_retries": 0, "condenser": "None", "max_iteration_per_run": _LIMIT + 1}
    assert job.calls == _LIMIT + 1
    assert job.statuses == [200] * (_LIMIT + 1)
    assert job.denied == []
    assert job.published == [("FINISHED", None)]
    assert job.stored[0]["agent_completed"] is True
    assert "agent_stop" not in job.stored[0]
