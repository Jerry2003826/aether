"""The threat-browser containers run through the Docker SDK on a runner with no docker CLI.

Real Docker, the real browser and Tor images, and PATH emptied so no `docker` binary can be found
(an SDK-only runner: SDK and socket, no CLI). The launch commands threat_launch builds are run by its
docker_runner / docker_cleanup; inside the browser container a probe checks that every flag took
effect, and afterwards nothing is left behind. The browser container's HostConfig is inspected through
the SDK right before docker_runner removes it, so its limits are checked whatever the host's cgroups.
In-container cgroup values must be present and right on cgroup v2; only a daemon that reports
cgroup v1 skips them, and says so in the test output.
"""

from __future__ import annotations

import json
import warnings

import docker
import pytest
from aether_worker import container_runtime
from aether_worker.threat_launch import (
    browser_command,
    cleanup_commands,
    container_name,
    docker_cleanup,
    docker_runner,
    link_binding,
    setup_commands,
)
from aether_worker.threat_pins import APPROVED_TOR_PROXY, TOR_ALIAS
from docker.errors import NotFound

from tests.integration.test_threat_browser_container import _build, _require_docker

_PROBE = r"""
import json, os, socket, sys
payload = json.load(sys.stdin)
def writable(path):
    try:
        with open(os.path.join(path, ".probe"), "w") as handle:
            handle.write("x")
        os.unlink(os.path.join(path, ".probe"))
        return True
    except OSError:
        return False
status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
routes = [line.split()[1] for line in open("/proc/net/route").read().splitlines()[1:]]
def cgroup(name):
    try:
        return open("/sys/fs/cgroup/" + name).read().strip()
    except OSError:
        return None
shm = os.statvfs("/dev/shm")
tmp = os.statvfs("/tmp")
print(json.dumps({
    "echo": payload,
    "uid": os.getuid(),
    "root_writable": writable("/"),
    "tmp_writable": writable("/tmp"),
    "cap_eff": status["CapEff"].strip(),
    "no_new_privs": status["NoNewPrivs"].strip(),
    "default_route": "00000000" in routes,
    "all_proxy": os.environ.get("ALL_PROXY"),
    "cleared": {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")},
    "tor_alias": socket.gethostbyname("__TOR_ALIAS__"),
    "pids_max": cgroup("pids.max"),
    "memory_max": cgroup("memory.max"),
    "swap_max": cgroup("memory.swap.max"),
    "shm_bytes": shm.f_frsize * shm.f_blocks,
    "tmp_bytes": tmp.f_frsize * tmp.f_blocks,
    "cgroup_v2": os.path.exists("/sys/fs/cgroup/cgroup.controllers"),
}))
""".replace("__TOR_ALIAS__", TOR_ALIAS)


def _gone(client: docker.DockerClient, kind: str, name: str) -> bool:
    try:
        (client.containers if kind == "container" else client.networks).get(name)
    except NotFound:
        return True
    return False


def test_threat_containers_run_through_the_sdk_without_a_docker_cli(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _require_docker()
    browser = _build("infra/threat-browser.Dockerfile")
    tor = _build("infra/tor.Dockerfile")
    client = docker.from_env()
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # no docker binary anywhere on PATH
    name = container_name()
    subnet, _gateway, address = link_binding(name)
    cgroup_version = str(client.info().get("CgroupVersion") or "")
    # docker_runner's own SDK client, with every container it removes inspected (real Engine API) first.
    sdk = docker.from_env()
    inspected: dict[str, dict] = {}
    remove = sdk.api.remove_container

    def inspect_then_remove(container, **kwargs):
        attrs = sdk.api.inspect_container(container)
        inspected[attrs["Name"].lstrip("/")] = attrs
        return remove(container, **kwargs)

    monkeypatch.setattr(sdk.api, "remove_container", inspect_then_remove)
    monkeypatch.setattr(container_runtime, "_CLIENT", sdk)
    try:
        for command in setup_commands(name, tor_image=tor, fixture=True):
            docker_runner(command, b"")
        tor_info = client.containers.get(f"{name}-tor").attrs
        link = tor_info["NetworkSettings"]["Networks"][f"{name}-link"]
        assert link["IPAddress"] == address
        assert TOR_ALIAS in (link.get("Aliases") or [])
        assert f"{name}-egress" in tor_info["NetworkSettings"]["Networks"]
        assert tor_info["HostConfig"]["AutoRemove"] is True
        assert tor_info["HostConfig"]["CapDrop"] == ["ALL"]
        assert client.networks.get(f"{name}-link").attrs["Internal"] is True
        assert client.networks.get(f"{name}-link").attrs["IPAM"]["Config"][0]["Subnet"] == subnet
        assert docker_runner(["exec", f"{name}-tor", "python3", "-c", "print('ready')"], b"").strip() == b"ready"

        payload = {"url": "https://example.com/note", "browser_enabled": True}
        raw = docker_runner(
            browser_command(name, image=browser, use_tor_proxy=True, command=("python3", "-c", _PROBE)),
            json.dumps(payload).encode(),
        )
        report = json.loads(raw)
        assert report["echo"] == payload
        assert report["uid"] != 0
        assert report["root_writable"] is False and report["tmp_writable"] is True
        assert int(report["cap_eff"], 16) == 0
        assert report["no_new_privs"] == "1"
        assert report["default_route"] is False
        assert report["all_proxy"] == APPROVED_TOR_PROXY
        assert report["cleared"] == {"HTTP_PROXY": "", "HTTPS_PROXY": "", "NO_PROXY": ""}
        assert report["tor_alias"] == address
        assert set(inspected) == {f"{name}-browser"}, sorted(inspected)
        host_config = inspected[f"{name}-browser"]["HostConfig"]
        assert host_config["PidsLimit"] == 128
        assert host_config["Memory"] == 1073741824
        assert host_config["MemorySwap"] == 1073741824
        if cgroup_version == "1":
            message = "cgroup v1: 跳过容器内 cgroup 限制断言（HostConfig 限制已断言）"
            print(message)
            warnings.warn(message, stacklevel=1)
        else:
            # cgroup v2 (or a daemon that does not say v1): the values must be there and right.
            assert cgroup_version == "2", f"Docker 报告的 cgroup 版本无法确认: {cgroup_version!r}"
            assert report["cgroup_v2"] is True, report
            assert report["pids_max"] == "128", report
            assert report["memory_max"] == "1073741824", report
            assert report["swap_max"] == "0", report
            print("cgroup v2: 已断言容器内 cgroup 限制")
        assert report["shm_bytes"] == 268435456
        assert report["tmp_bytes"] == 268435456
        assert _gone(client, "container", f"{name}-browser")  # --rm
    finally:
        for command in cleanup_commands(name, tor=True):
            docker_cleanup(command, b"")
    assert _gone(client, "container", f"{name}-tor")
    assert _gone(client, "network", f"{name}-link")
    assert _gone(client, "network", f"{name}-egress")
