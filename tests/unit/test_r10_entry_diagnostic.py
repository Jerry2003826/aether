"""An unexpected entry error keeps its type as the case detail, logs its message on one bounded
stderr line, and the worker redacts secrets in both log tails before they are stored."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from aether_worker import coding_sandbox

_ENTRY_DIR = Path("services/worker/aether_worker").resolve()
_PYTHON = Path("environments/openhands/.venv/bin/python")
# Readable fixture phrase, not a credential.
_FIXTURE_PASSWORD = "this-is-a-fixture-password"


def _entry(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    code = f"import sys\nsys.path.append({str(_ENTRY_DIR)!r})\nimport openhands_sandbox_entry as entry\n" + snippet
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AETHER_SANDBOX_WORK": str(tmp_path / "work")}
    return subprocess.run(
        [str(_PYTHON.absolute()), "-I", "-c", code], cwd=tmp_path, env=env, capture_output=True, timeout=120, check=False
    )


def _crash(message: str, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    snippet = (
        "def boom():\n"
        f"    raise RuntimeError({message!r})\n"
        "entry.become_subreaper = boom\n"
        "entry.main()\n"
    )
    return _entry(snippet, tmp_path)


def test_an_unexpected_error_logs_its_message_and_keeps_the_type_as_detail(tmp_path: Path) -> None:
    completed = _crash("[keep] copy failed\nsecond line", tmp_path)
    assert completed.returncode == 2
    result = json.loads(completed.stdout.decode().splitlines()[-1].removeprefix("RESULT "))
    assert result["cases"][0]["detail"] == "RuntimeError"
    lines = [line for line in completed.stderr.decode().splitlines() if line.startswith("entry_failed ")]
    assert lines == ["entry_failed RuntimeError: [keep] copy failed second line"]


def test_the_logged_message_is_bounded(tmp_path: Path) -> None:
    completed = _crash("x" * 5000, tmp_path)
    assert completed.returncode == 2
    lines = [line for line in completed.stderr.decode().splitlines() if line.startswith("entry_failed ")]
    assert len(lines) == 1
    assert lines[0].startswith("entry_failed RuntimeError: xxx")
    assert len(lines[0]) <= len("entry_failed RuntimeError: ") + 500


def _docker(code: str):
    def docker(args, *, timeout=None):
        if args[:3] == ["inspect", "-f", "{{.State.ExitCode}}"]:
            return subprocess.CompletedProcess(args, 0, code + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return docker


def _logs(monkeypatch, stdout: bytes, stderr: bytes, code: str) -> None:
    monkeypatch.setattr(coding_sandbox, "worker_delegates", lambda: False)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(coding_sandbox, "container_logs", lambda _name, *, limit: (stdout[-limit:], stderr[-limit:]))
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", _docker(code))


_LEAK = f"entry_failed OSError: postgresql://aether:{_FIXTURE_PASSWORD}@db:5432/aether\n".encode()


def test_a_crashed_entry_has_secrets_redacted_from_its_tails(monkeypatch) -> None:
    result = {"passed": 0, "failed": 1, "errors": 1, "cases": [{"name": "runner", "verdict": "FAIL", "detail": "OSError"}]}
    stdout = b"note password=" + _FIXTURE_PASSWORD.encode() + b"\nRESULT " + json.dumps(result).encode() + b"\n"
    _logs(monkeypatch, stdout, b"before\n" + _LEAK + b"after\n", "2")
    with pytest.raises(coding_sandbox.CodingSandboxError) as caught:
        coding_sandbox._collect_exit("aether-code-diag1")
    logs = caught.value.payload
    assert _FIXTURE_PASSWORD not in logs["stderr"] and _FIXTURE_PASSWORD not in logs["stdout"]
    assert "postgresql://aether:[REDACTED]@db:5432/aether" in logs["stderr"]
    assert logs["stderr"].startswith("before\n") and logs["stderr"].endswith("after\n")


def test_a_graded_exit_has_secrets_redacted_from_its_tails(monkeypatch) -> None:
    result = {"passed": 1, "failed": 0, "errors": 0, "cases": [], "patch": f"password={_FIXTURE_PASSWORD}"}
    stdout = ("RESULT " + json.dumps(result) + "\n").encode()
    _logs(monkeypatch, stdout, _LEAK, "1")
    collected = coding_sandbox._collect_exit("aether-code-diag2")
    assert _FIXTURE_PASSWORD not in collected["stderr_tail"] and _FIXTURE_PASSWORD not in collected["stdout_tail"]
    # The result itself is parsed from the raw capture: redaction only touches the stored log tails.
    assert collected["result"]["patch"] == f"password={_FIXTURE_PASSWORD}"


def test_timeout_log_tails_are_redacted(monkeypatch) -> None:
    _logs(monkeypatch, b"", _LEAK, "137")
    logs = coding_sandbox.container_log_tails("job", "aether-code-diag3")
    assert _FIXTURE_PASSWORD not in logs["stderr"]
