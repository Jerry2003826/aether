"""Every container that runs candidate or job code drops all capabilities and forbids new privileges."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aether_worker import f26_acceptance, runner, verification


def _has(command: list[str], flag: str, value: str) -> bool:
    return any(command[index] == flag and command[index + 1] == value for index in range(len(command) - 1))


def _locked(command: list[str]) -> bool:
    return _has(command, "--cap-drop", "ALL") and _has(command, "--security-opt", "no-new-privileges")


def _record_runner(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def docker(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        seen.append(list(args))
        code = 1 if args[:1] == ["inspect"] else 0
        return subprocess.CompletedProcess(args, code, "", "")

    monkeypatch.setattr(runner, "_docker", docker)
    monkeypatch.setattr(runner, "_ensure_image", lambda: None)
    monkeypatch.setattr(runner, "_container_id", lambda _key: "cid-1")
    runner._launch_container("job-1", token="probe-token", provider_key="")
    return [args for args in seen if args[:1] == ["run"]]


def test_the_runner_job_container_drops_all_capabilities_and_forbids_new_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = [args for args in _record_runner(monkeypatch) if "aether.role=job" in args]
    assert len(job) == 1 and _locked(job[0]), job
    assert "--cap-add" not in job[0], job


def test_the_runner_gateway_keeps_only_net_admin_and_forbids_new_privileges(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = [args for args in _record_runner(monkeypatch) if "aether.role=gateway" in args]
    assert len(gateway) == 1 and _locked(gateway[0]), gateway
    added = [gateway[0][index + 1] for index, part in enumerate(gateway[0]) if part == "--cap-add"]
    assert added == ["NET_ADMIN"], gateway


def test_the_grader_container_drops_all_capabilities_and_forbids_new_privileges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def docker(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(verification, "_docker", docker)
    verification.run_grader_container(tmp_path, timeout_seconds=None)
    runs = [args for args in seen if args[:1] == ["run"]]
    assert len(runs) == 1 and _locked(runs[0]), runs


def test_the_f26_verify_container_drops_all_capabilities_and_forbids_new_privileges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _locked(list(f26_acceptance.VERIFY_FLAGS))
    seen: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, "PASS\n", "")

    monkeypatch.setattr(f26_acceptance.subprocess, "run", run)
    f26_acceptance._docker_check(tmp_path, "")
    assert seen and all(_locked(command) for command in seen), seen


def test_the_grader_container_adds_back_exactly_the_capabilities_its_root_pid_1_needs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The grader runs as root (PID 1) over a host-owned 0700 bind mount and drops probes to nobody:
    # it must read /work (DAC_OVERRIDE), chown the child home (CHOWN), setuid/setgid/setgroups to
    # nobody (SETUID, SETGID) and reap nobody's leftovers with kill(-1) (KILL). Nothing else.
    seen: list[list[str]] = []

    def docker(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(verification, "_docker", docker)
    verification.run_grader_container(tmp_path, timeout_seconds=None)
    (command,) = [args for args in seen if args[:1] == ["run"]]
    assert _locked(command), command
    added = sorted(command[index + 1] for index, part in enumerate(command) if part == "--cap-add")
    assert added == ["CHOWN", "DAC_OVERRIDE", "KILL", "SETGID", "SETUID"], command
    assert "--privileged" not in command and "--user" not in command, command
    # Every --cap-add comes after the --cap-drop ALL it narrows.
    assert all(command.index("--cap-drop") < index for index, part in enumerate(command) if part == "--cap-add"), command
