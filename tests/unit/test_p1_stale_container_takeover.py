"""A worker taking over a job must not reuse a container still wired to the dead worker's proxy.

Each run gets a fresh proxy directory and model socket, bind-mounted into the container at start.
A container left running by a worker that died still talks to the old socket, so the next worker
removes it and starts one on its own proxy. A container this run already started on its current
proxy (the same proxy directory and job token, re-entering the launch) is still reused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aether_worker import coding_jobs, coding_sandbox
from aether_worker.coding_sandbox import CodingSandboxError, run_coding_sandbox

_OBJECTIVE = "Make value() return 42."
_PROXY_MOUNT = ":/run/aether/proxy:rw"


class _FakeDocker:
    """Containers by id, with the labels, proxy mount and job token they were started with."""

    def __init__(self, *, removable: bool = True) -> None:
        self.removable = removable
        self.containers: dict[str, dict[str, Any]] = {}
        self.commands: list[list[str]] = []

    def add(self, cid: str, *, job_id: str, proxy: Path, token: str) -> None:
        self.containers[cid] = {
            "name": coding_sandbox.container_name(job_id),
            "job": job_id,
            "proxy": str(proxy.resolve()),
            "token": token,
            "status": "running",
        }

    def ids(self, job_id: str) -> list[str]:
        return [cid for cid, item in self.containers.items() if item["job"] == job_id]

    def _find(self, target: str) -> list[str]:
        return [cid for cid, item in self.containers.items() if target in {cid, item["name"]}]

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(args))
        if args[:1] == ["run"]:
            return self._run(args)
        if args[:2] == ["rm", "-f"]:
            if not self.removable:
                return subprocess.CompletedProcess(["docker"], 1, "", "removal refused")
            for cid in self._find(args[2]):
                del self.containers[cid]
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if args[:2] == ["stop", "-t"]:
            for cid in self._find(args[3]):
                self.containers[cid]["status"] = "exited"
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if args[:1] == ["ps"]:
            job = next(part.split("=", 1)[1] for part in args if part.startswith("label=aether.coding="))
            return subprocess.CompletedProcess(["docker"], 0, "".join(f"{cid}\n" for cid in self.ids(job)), "")
        if args[:1] == ["inspect"]:
            found = self._find(args[-1])
            if not found:
                return subprocess.CompletedProcess(["docker"], 1, "", "missing")
            return subprocess.CompletedProcess(["docker"], 0, self.containers[found[0]]["status"] + "\n", "")
        return subprocess.CompletedProcess(["docker"], 1, "", "unmapped")

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        name = args[args.index("--name") + 1]
        if self._find(name):
            return subprocess.CompletedProcess(["docker"], 125, "", "name already in use")
        job = next(part.split("=", 1)[1] for part in args if part.startswith("aether.coding="))
        proxy = next(part[: -len(_PROXY_MOUNT)] for part in args if part.endswith(_PROXY_MOUNT))
        token = next(part.split("=", 1)[1] for part in args if part.startswith("AETHER_JOB_TOKEN="))
        cid = f"cid{len(self.commands)}"
        self.containers[cid] = {"name": name, "job": job, "proxy": proxy, "token": token, "status": "running"}
        return subprocess.CompletedProcess(["docker"], 0, cid + "\n", "")

    def runs(self) -> list[list[str]]:
        return [command for command in self.commands if command[:1] == ["run"]]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeDocker, list[str]]:
    fake = _FakeDocker()
    waited: list[str] = []
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setattr(coding_sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(coding_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(coding_sandbox, "launch_image", lambda image, _default: image)
    monkeypatch.setattr(coding_sandbox, "coding_container_ids", lambda job_id: fake.ids(job_id))
    monkeypatch.setattr(coding_sandbox, "_docker", fake)
    monkeypatch.setattr(coding_sandbox, "_sandbox_docker", fake)
    monkeypatch.setattr(coding_sandbox, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _s: None))

    def wait(job_id: str, **_kwargs: Any) -> dict[str, Any]:
        waited.append(job_id)
        return {"exit_code": 0, "result": {}}

    monkeypatch.setattr(coding_sandbox, "_wait_until_stopped", wait)
    return fake, waited


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "aether-code-work"
    old = tmp_path / "aether-model-old"
    new = tmp_path / "aether-model-new"
    for item in (workspace, old, new):
        item.mkdir()
    return workspace, old, new


def test_a_container_left_on_the_dead_workers_proxy_is_replaced_on_takeover(docker: tuple[_FakeDocker, list[str]], tmp_path: Path) -> None:
    fake, waited = docker
    job = "00000000-0000-0000-0000-0000000000d1"
    workspace, old, new = _dirs(tmp_path)
    # Started by the worker that died: its proxy directory and job token, not this run's.
    fake.add("cid-old", job_id=job, proxy=old, token="token-old-worker")
    run_coding_sandbox(workspace, objective=_OBJECTIVE, job_id=job, proxy_dir=new, job_token="token-new-worker")
    assert "cid-old" not in fake.containers, "the container wired to the dead worker's proxy was reused"
    assert [item["proxy"] for item in fake.containers.values()] == [str(new.resolve())]
    assert [item["token"] for item in fake.containers.values()] == ["token-new-worker"]
    assert len(fake.runs()) == 1
    assert waited == [job]


def test_a_container_on_the_current_proxy_is_reused_when_the_launch_is_entered_again(docker: tuple[_FakeDocker, list[str]], tmp_path: Path) -> None:
    fake, waited = docker
    job = "00000000-0000-0000-0000-0000000000d2"
    workspace, _old, new = _dirs(tmp_path)
    run_coding_sandbox(workspace, objective=_OBJECTIVE, job_id=job, proxy_dir=new, job_token="token-this-run")
    started = dict(fake.containers)
    assert len(started) == 1
    run_coding_sandbox(workspace, objective=_OBJECTIVE, job_id=job, proxy_dir=new, job_token="token-this-run")
    assert fake.containers == started
    assert len(fake.runs()) == 1
    assert not [command for command in fake.commands if command[:1] in (["rm"], ["stop"])]
    assert waited == [job, job]


def test_a_stale_container_that_cannot_be_removed_is_not_reused_and_the_job_stays_open(
    docker: tuple[_FakeDocker, list[str]], tmp_path: Path
) -> None:
    fake, waited = docker
    fake.removable = False
    job = "00000000-0000-0000-0000-0000000000d3"
    workspace, old, new = _dirs(tmp_path)
    fake.add("cid-old", job_id=job, proxy=old, token="token-old-worker")
    with pytest.raises(CodingSandboxError) as stopped:
        run_coding_sandbox(workspace, objective=_OBJECTIVE, job_id=job, proxy_dir=new, job_token="token-new-worker")
    assert stopped.value.code == "stale_container_unconfirmed"
    # Not terminal: the lease is cleared and the next claim retries the removal.
    assert stopped.value.code in coding_jobs._UNCONFIRMED
    assert fake.runs() == []
    assert waited == []
