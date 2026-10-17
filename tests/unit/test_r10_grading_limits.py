"""Grading limits: a hung or slow suite is a graded failure, never a tampering verdict; no grading
process outlives the grader; the coding sandbox keeps every capability dropped."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from aether_worker import grading_files
from aether_worker.coding_jobs import GRADING_VIOLATIONS, _coding_terminal
from aether_worker.coding_sandbox import container_arguments
from aether_worker.container_runtime import _parse_run
from aether_worker.live_harness import _isolation_command

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env.update(extra)
    return env


def _grade(root: Path, **extra: str) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=root,
        env=_env(**extra),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    line = next(item for item in reversed(completed.stdout.splitlines()) if item.startswith("{"))
    return completed.returncode, json.loads(line)


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


# ---------------------------------------------------------------------------------------------
# Timeout is not tampering.


def test_a_hung_test_is_a_timed_out_failure_not_a_violation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": "import time\ndef value():\n    time.sleep(3600)\n    return 42\ndef quick():\n    return 42\n",
            "test_app.py": (
                "import app\n\n"
                "def test_hangs():\n    assert app.value() == 42\n\n"
                "def test_quick():\n    assert app.quick() == 42\n"
            ),
        },
    )
    started = time.monotonic()
    code, body = _grade(tmp_path, AETHER_PROTOCOL_TEST_TIMEOUT_SECONDS="2")
    assert time.monotonic() - started < 30, body
    cases = {case["name"]: (case["verdict"], case["detail"]) for case in body["cases"]}
    assert cases["test_app.test_hangs"] == ("FAIL", "timeout"), body
    assert cases["test_app.test_quick"] == ("PASS", ""), body
    assert code == 1
    assert not any(case["detail"] in GRADING_VIOLATIONS or case["detail"].startswith("grading_") for case in body["cases"]), body


def test_grading_timeout_is_its_own_failure_code_and_never_a_violation() -> None:
    assert "grading_timeout" not in GRADING_VIOLATIONS
    assert "grading_unverified" in GRADING_VIOLATIONS
    sandbox = {
        "exit_code": 1,
        "result": {
            "passed": 0,
            "failed": 1,
            "errors": 1,
            "cases": [{"name": "runner", "verdict": "FAIL", "detail": "grading_timeout"}],
            "agent_completed": True,
            "model_requests": 1,
            "patch": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
            "network_errno": 101,
            "credential_present": False,
        },
    }
    assert _coding_terminal(sandbox, upstream_error=None, measured=(3, 4), usage_missing=False, channel_revoked=True) == ("FAILED", "grading_timeout")


# ---------------------------------------------------------------------------------------------
# The overall limit grows with the suite, under a hard cap; one test's limit is always below it.


def test_the_protocol_limit_grows_with_the_suite_and_stays_above_one_test() -> None:
    per_test = grading_files.PROTOCOL_TEST_TIMEOUT_SECONDS
    cap = grading_files.PROTOCOL_CAP_SECONDS
    for steps in (0, 1, 2, 10, 100, 1000, 100000):
        limit = grading_files.protocol_timeout_seconds(steps)
        assert per_test < limit <= cap, (steps, limit)
        assert limit == min(cap, grading_files.PROTOCOL_BASE_SECONDS + grading_files.PROTOCOL_STEP_SECONDS * steps)
    assert grading_files.PROTOCOL_CALL_TIMEOUT_SECONDS < per_test


def test_a_suite_whose_isolation_overhead_exceeds_40_seconds_gets_a_longer_limit(tmp_path: Path) -> None:
    # 600 tests in 20 modules: one fresh process per test is roughly 0.1 s each, well past 40 s in total.
    for index in range(20):
        body = "".join(f"def test_{index}_{case}():\n    assert True\n\n" for case in range(30))
        _write(tmp_path, {f"tests/test_m{index}.py": body})
    steps = grading_files.grading_steps(tmp_path)
    assert steps == 620, steps
    assert grading_files.protocol_timeout_seconds(steps) > 0.1 * steps + 20


def test_the_sandbox_entry_bounds_the_protocol_by_the_suite_not_a_constant() -> None:
    import ast

    source = Path("services/worker/aether_worker/openhands_sandbox_entry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute_command"]
    assert calls
    for call in calls:
        timeout = next(keyword.value for keyword in call.keywords if keyword.arg == "timeout")
        assert not isinstance(timeout, ast.Constant), ast.unparse(timeout)
    assert "protocol_timeout_seconds" in source and "grading_steps" in source


# ---------------------------------------------------------------------------------------------
# No test or candidate process survives the grader.


_HANG = (
    "import os, pathlib, time\n"
    "def value():\n"
    "    pathlib.Path(os.environ.get('PIDS_DIR', '.')).joinpath('pids').write_text(f'{os.getpid()} {os.getppid()}')\n"
    "    time.sleep(3600)\n"
)


def _start_hung(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int, int]:
    work = tmp_path / "work"
    _write(work, {"app.py": _HANG, "test_app.py": "import app\n\ndef test_hangs():\n    app.value()\n"})
    grader = subprocess.Popen(
        [sys.executable, "-I", str(_PROTOCOL)],
        cwd=work,
        env=_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    marker = work / "pids"
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the candidate never started"
    candidate, runner = (int(item) for item in marker.read_text(encoding="utf-8").split())
    assert _alive(candidate) and _alive(runner)
    return grader, candidate, runner


def _assert_all_gone(*pids: int) -> None:
    deadline = time.monotonic() + 10
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    survivors = [pid for pid in pids if _alive(pid)]
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    assert not survivors, f"processes outlived the grader: {survivors}"


def test_sigterm_to_the_grader_stops_every_test_and_candidate_process(tmp_path: Path) -> None:
    grader, candidate, runner = _start_hung(tmp_path)
    grader.send_signal(signal.SIGTERM)
    grader.wait(timeout=10)
    _assert_all_gone(candidate, runner)


def test_sigint_to_the_grader_stops_every_test_and_candidate_process(tmp_path: Path) -> None:
    grader, candidate, runner = _start_hung(tmp_path)
    grader.send_signal(signal.SIGINT)
    grader.wait(timeout=10)
    _assert_all_gone(candidate, runner)


def test_a_killed_grader_leaves_no_test_or_candidate_process(tmp_path: Path) -> None:
    # SIGKILL is what the sandbox entry's timeout sends; nothing in the grader can run on it.
    grader, candidate, runner = _start_hung(tmp_path)
    grader.kill()
    grader.wait(timeout=10)
    _assert_all_gone(candidate, runner)


# ---------------------------------------------------------------------------------------------
# The coding sandbox drops every capability (ptrace included) and forbids privilege gain.


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[index + 1] for index, token in enumerate(args) if token == flag]


def test_every_coding_sandbox_launch_drops_all_capabilities_and_forbids_new_privileges(tmp_path: Path) -> None:
    args = container_arguments(tmp_path, "aether-openhands-sandbox:test", objective="Fix app.py.", job_id="job-1", proxy_dir=tmp_path, job_token="t")
    assert _pairs(args, "--cap-drop") == ["ALL"], args
    assert _pairs(args, "--security-opt") == ["no-new-privileges"], args
    assert "--cap-add" not in args and "--privileged" not in args
    # The Docker SDK path (local and the remote runner, which both start from container_arguments).
    run = _parse_run(args[1:])
    assert run["kwargs"]["cap_drop"] == ["ALL"], run
    assert run["kwargs"]["security_opt"] == ["no-new-privileges"], run
    assert run["kwargs"]["cap_add"] is None, run


def test_the_remote_runner_starts_coding_containers_from_the_same_arguments() -> None:
    import ast

    source = Path("services/worker/aether_worker/runner_process.py").read_text(encoding="utf-8")
    run = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_run")
    called = {node.func.id for node in ast.walk(run) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "container_arguments" in called


def test_the_live_harness_isolation_probe_drops_all_capabilities_and_forbids_new_privileges() -> None:
    args = _isolation_command()[1:]
    assert _pairs(args, "--cap-drop") == ["ALL"], args
    assert _pairs(args, "--security-opt") == ["no-new-privileges"], args
    run = _parse_run(args[1:])
    assert run["kwargs"]["cap_drop"] == ["ALL"] and run["kwargs"]["security_opt"] == ["no-new-privileges"], run


def test_the_protocol_budget_comes_from_the_baseline_workspace_not_the_agents_copy() -> None:
    import ast

    source = Path("services/worker/aether_worker/openhands_sandbox_entry.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "grading_steps"
    ]
    assert calls, "the entry must size the protocol budget with grading_steps"
    # WORK is the read-only baseline the job was copied from; the agent never wrote to it.
    assert all(ast.unparse(call.args[0]) == "WORK" for call in calls), [ast.unparse(call) for call in calls]


def test_the_sandbox_container_outlasts_the_agent_and_the_protocol_cap_with_margin() -> None:
    from aether_worker.coding_sandbox import CODING_SANDBOX_TIMEOUT_SECONDS

    # 80 s agent share + protocol cap + the entry's 5 s kill margin + 30 s for start-up and the result.
    assert CODING_SANDBOX_TIMEOUT_SECONDS == 80 + grading_files.PROTOCOL_CAP_SECONDS + 5 + 30
