"""The threat-browser containers run through the Docker SDK on a runner with no docker CLI.

Real Docker, the real browser and Tor images, and PATH emptied so no `docker` binary can be found
(an SDK-only runner: SDK and socket, no CLI). The launch commands threat_launch builds are run by its
docker_runner / docker_cleanup; inside the browser container a probe checks that every flag took
effect, and afterwards nothing is left behind.
"""

from __future__ import annotations

import json

import docker
import pytest
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
        if report["pids_max"] is not None:
            assert report["pids_max"] == "128"
        if report["memory_max"] is not None:
            assert report["memory_max"] == "1073741824"
        if report["swap_max"] is not None:
            assert report["swap_max"] == "0"
        assert report["shm_bytes"] == 268435456
        assert report["tmp_bytes"] == 268435456
        assert _gone(client, "container", f"{name}-browser")  # --rm
    finally:
        for command in cleanup_commands(name, tor=True):
            docker_cleanup(command, b"")
    assert _gone(client, "container", f"{name}-tor")
    assert _gone(client, "network", f"{name}-link")
    assert _gone(client, "network", f"{name}-egress")
