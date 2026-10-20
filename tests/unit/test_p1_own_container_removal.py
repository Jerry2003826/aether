"""A worker that gives up a run removes only the container it started, never one started after it.

The container name and the job labels are the same for every worker on a job. When the old
worker is still alive but lost its lease, the new worker has already replaced the old container
(by name, on takeover) and started its own under the same name and labels. The old worker's next
poll then fails to renew, sees a cancel, reaches the deadline or times out; whatever it removes on
the way out must be the container id it started, so the new worker's container keeps running.
Only the takeover replacement removes by name.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aether_worker import coding_sandbox, runner_process, sandbox_delegate
from aether_worker.coding_sandbox import CodingSandboxError, run_coding_sandbox

_OBJECTIVE = "Make value() return 42."
_JOB = "00000000-0000-0000-0000-0000000000e1"
_PROXY_MOUNT = ":/run/aether/proxy:rw"


class _LeaseLost(Exception):
    pass


class _Docker:
    """Containers by full 64-hex id; `ps -q` lists 12-character short ids like the real CLI."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, str]] = {}
        self.commands: list[list[str]] = []
        self._serial = 0

    def _new_id(self) -> str:
        self._serial += 1
        return f"{self._serial:02x}" * 32

    def _find(self, target: str) -> list[str]:
        return [
            cid
            for cid, item in self.containers.items()
            if target == item["name"] or (len(target) >= 12 and cid.startswith(target))
        ]

    def start(self, *, job_id: str, proxy: str, token: str) -> str:
        cid = self._new_id()
        self.containers[cid] = {
            "name": coding_sandbox.container_name(job_id),
            "job": job_id,
            "proxy": proxy,
            "token": token,
            "status": "running",
        }
        return cid

    def take_over(self, job_id: str, *, proxy: str, token: str) -> str:
        """What the new worker does to Docker on takeover: remove by name and label, start its own."""
        name = coding_sandbox.container_name(job_id)
        for cid in [cid for cid, item in self.containers.items() if item["name"] == name or item["job"] == job_id]:
            del self.containers[cid]
        return self.start(job_id=job_id, proxy=proxy, token=token)

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(args))
        if args[:1] == ["run"]:
            name = args[args.index("--name") + 1]
            if self._find(name):
                return subprocess.CompletedProcess(["docker"], 125, "", "name already in use")
            job = next(part.split("=", 1)[1] for part in args if part.startswith("aether.coding="))
            proxy = next(part[: -len(_PROXY_MOUNT)] for part in args if part.endswith(_PROXY_MOUNT))
            token = next(part.split("=", 1)[1] for part in args if part.startswith("AETHER_JOB_TOKEN="))
            cid = self.start(job_id=job, proxy=proxy, token=token)
            return subprocess.CompletedProcess(["docker"], 0, cid + "\n", "")
        if args[:2] == ["rm", "-f"]:
            found = self._find(args[2])
            for cid in found:
                del self.containers[cid]
            if not found:
                return subprocess.CompletedProcess(["docker"], 1, "", "No such container")
            return subprocess.CompletedProcess(["docker"], 0, args[2] + "\n", "")
        if args[:2] == ["stop", "-t"]:
            found = self._find(args[3])
            for cid in found:
                self.containers[cid]["status"] = "exited"
            return subprocess.CompletedProcess(["docker"], 0 if found else 1, "", "" if found else "No such container")
        if args[:2] == ["ps", "-aq"]:
            job = next(part.split("=", 1)[1] for part in args if part.startswith("label=aether.coding="))
            listed = [cid[:12] for cid, item in self.containers.items() if item["job"] == job]
            return subprocess.CompletedProcess(["docker"], 0, "".join(f"{cid}\n" for cid in listed), "")
        if args[:1] == ["inspect"]:
            found = self._find(args[-1])
            if not found:
                return subprocess.CompletedProcess(["docker"], 1, "", "No such object")
            item = self.containers[found[0]]
            value = item["status"] if "Status" in args[2] else "0"
            return subprocess.CompletedProcess(["docker"], 0, value + "\n", "")
        return subprocess.CompletedProcess(["docker"], 1, "", "unmapped")

    def ours(self, cid: str) -> list[list[str]]:
        """Commands from the old worker that touched cid or the shared name (after the takeover)."""
        name = self.containers[cid]["name"] if cid in self.containers else ""
        return [
            command
            for command in self.commands
            if command[:1] in (["rm"], ["stop"]) and (command[-1] == name or (len(command[-1]) >= 12 and cid.startswith(command[-1])))
        ]


@pytest.fixture
def dirs() -> Iterator[tuple[Path, Path]]:
    # Real temp directories with the prefixes the runner's accepted_workspace/accepted_proxy require.
    workspace = Path(tempfile.mkdtemp(prefix="aether-code-"))
    proxy = Path(tempfile.mkdtemp(prefix="aether-model-"))
    try:
        yield workspace, proxy
    finally:
        workspace.rmdir()
        proxy.rmdir()


def _wire(monkeypatch: pytest.MonkeyPatch, *, delegated: bool) -> _Docker:
    fake = _Docker()
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(coding_sandbox, "launch_image", lambda image, _default: image)
    monkeypatch.setattr(coding_sandbox, "_bounded_logs", lambda *_a, **_k: {"stdout": "", "stderr": ""})
    monkeypatch.setattr(coding_sandbox, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _s: None))
    monkeypatch.setattr(coding_sandbox, "_STARTED_ON", {})
    if delegated:
        # The worker role never runs docker itself: every call goes through the runner's fixed actions.
        monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
        monkeypatch.setattr(coding_sandbox, "_docker", lambda *_a, **_k: pytest.fail("local docker in the worker role"))
        monkeypatch.setattr(runner_process, "_docker", fake)
        monkeypatch.setattr(runner_process, "launch_image", lambda image, _default: image)

        def ask(op: str, fields: dict, *, timeout: float | None) -> dict:
            status, body = runner_process._sandbox({"op": op, **fields})
            if body.get("error"):
                raise sandbox_delegate.SandboxDelegateError(str(body["error"]))
            assert status == 200, body
            return body

        monkeypatch.setattr(sandbox_delegate, "_ask", ask)
    else:
        monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
        monkeypatch.setattr(coding_sandbox, "_docker", fake)
    return fake


def _old_worker_run(
    fake: _Docker,
    workspace: Path,
    proxy: Path,
    *,
    lose: str,
) -> tuple[BaseException, str, str]:
    """The old worker runs; on its first poll the new worker takes over, then the old one gives up."""
    taken: dict[str, str] = {}
    old_started: list[str] = []

    def on_poll() -> None:
        if not taken:
            old_started.extend(fake.containers)
            taken["new"] = fake.take_over(_JOB, proxy="/tmp/aether-model-new-worker", token="token-new-worker")
            fake.commands.clear()
            if lose == "lease":
                raise _LeaseLost(_JOB)

    flags: dict[str, Callable[[], bool]] = {
        "stop_requested": lambda: bool(taken) and lose == "cancel",
        "deadline_reached": lambda: bool(taken) and lose == "deadline",
    }
    with pytest.raises((CodingSandboxError, _LeaseLost)) as stopped:
        run_coding_sandbox(
            workspace,
            objective=_OBJECTIVE,
            job_id=_JOB,
            proxy_dir=proxy,
            job_token="token-old-worker",
            on_poll=on_poll,
            timeout_seconds=0 if lose == "timeout" else 1000,
            grace_seconds=1,
            poll_seconds=0,
            **flags,
        )
    assert len(old_started) == 1
    return stopped.value, old_started[0], taken["new"]


@pytest.mark.parametrize("delegated", [False, True], ids=["local", "delegated"])
@pytest.mark.parametrize("lose", ["lease", "cancel", "deadline", "timeout"])
def test_the_old_worker_never_removes_the_container_the_new_worker_started(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path], lose: str, delegated: bool
) -> None:
    fake = _wire(monkeypatch, delegated=delegated)
    workspace, proxy = dirs
    _error, old, new = _old_worker_run(fake, workspace, proxy, lose=lose)
    assert old not in fake.containers, "the takeover removed the old container"
    assert new in fake.containers, f"the old worker removed the new worker's container after losing it ({lose})"
    assert fake.containers[new]["status"] == "running", f"the old worker stopped the new worker's container ({lose})"
    assert fake.ours(new) == [], f"the old worker sent a stop or rm that targets the new container or the shared name ({lose})"


@pytest.mark.parametrize("delegated", [False, True], ids=["local", "delegated"])
def test_a_lost_lease_still_removes_the_container_this_worker_started(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path], delegated: bool
) -> None:
    fake = _wire(monkeypatch, delegated=delegated)
    workspace, proxy = dirs
    polls: list[int] = []

    def on_poll() -> None:
        polls.append(1)
        if len(polls) == 2:
            raise _LeaseLost(_JOB)

    with pytest.raises(_LeaseLost):
        run_coding_sandbox(
            workspace,
            objective=_OBJECTIVE,
            job_id=_JOB,
            proxy_dir=proxy,
            job_token="token-old-worker",
            on_poll=on_poll,
            grace_seconds=1,
            poll_seconds=0,
        )
    assert fake.containers == {}, "nobody took over, so the worker's own container must be gone"
    removed = [command for command in fake.commands if command[:2] == ["rm", "-f"]]
    assert removed and all(command[2] != coding_sandbox.container_name(_JOB) for command in removed), removed


@pytest.mark.parametrize("delegated", [False, True], ids=["local", "delegated"])
def test_a_confirmed_cancel_of_this_workers_own_container_is_still_cancelled(
    monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path], delegated: bool
) -> None:
    fake = _wire(monkeypatch, delegated=delegated)
    workspace, proxy = dirs
    with pytest.raises(CodingSandboxError) as stopped:
        run_coding_sandbox(
            workspace,
            objective=_OBJECTIVE,
            job_id=_JOB,
            proxy_dir=proxy,
            job_token="token-this-worker",
            stop_requested=lambda: True,
            grace_seconds=1,
            poll_seconds=0,
        )
    assert stopped.value.code == "cancelled"
    assert fake.containers == {}


def test_the_runner_accepts_a_full_container_id_for_stop_and_rm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The delegate protocol already names a container by id: an older runner needs no change."""
    sent: list[list[str]] = []

    def docker(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(runner_process, "_docker", docker)
    cid = "ab" * 32
    assert runner_process._sandbox({"op": "stop", "grace": "1", "name": cid})[0] == 200
    assert runner_process._sandbox({"op": "rm", "name": cid})[0] == 200
    assert runner_process._sandbox({"op": "inspect_status", "name": cid})[0] == 200
    assert sent == [["stop", "-t", "1", cid], ["rm", "-f", cid], ["inspect", "-f", "{{.State.Status}}", cid]]
