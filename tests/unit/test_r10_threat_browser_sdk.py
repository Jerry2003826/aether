"""The runner's /v1/threat-browser drives Docker through the SDK, never the docker CLI.

Codex (runner.py:166): every other runner Docker call goes through the SDK (container_runtime), and
probe_docker_daemon enables the runner when only the SDK reaches the daemon; but threat_launch's
docker_runner / docker_cleanup ran `subprocess.run(["docker", ...])`, so on an SDK-only runner (SDK
and socket, no CLI) every threat-browser visit died as role_failed. Each CLI flag maps 1:1 onto the
Engine API call it stands for; these tests pin that mapping with a recording low-level client.
"""

from __future__ import annotations

import json
import struct
import subprocess
from typing import Any

import docker
import pytest
from aether_worker import container_runtime, runner_process, threat_launch
from aether_worker.threat_launch import link_binding
from aether_worker.threat_pins import APPROVED_TOR_PROXY, ENTRYPOINT, TOR_ALIAS
from docker.errors import DockerException, NotFound

BROWSER = "sha256:" + "b" * 64
TOR = "sha256:" + "c" * 64
RESULT = {"text": "可见正文", "browser_version": "1", "playwright_version": "1", "user_agent": "HeadlessChrome"}
CLEARED = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")


def _frames(stdout: bytes, stderr: bytes = b"") -> bytes:
    out = b""
    for stream, data in ((1, stdout), (2, stderr)):
        if data:
            out += struct.pack(">BxxxL", stream, len(data)) + data
    return out


class _Socket:
    def __init__(self, api: _API, data: bytes) -> None:
        self._api = api
        self._data = data
        self.sent = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def shutdown(self, _how: int) -> None:
        self._api.calls.append(("stdin_closed", self.sent))

    def settimeout(self, _value: float) -> None:
        return None

    def recv(self, size: int) -> bytes:
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def close(self) -> None:
        return None


class _API(docker.APIClient):
    """Real host/networking config builders; every daemon call is recorded instead of sent."""

    def __init__(self, *, browser_exit: int = 0, browser_stdout: bytes | None = None, exec_fails: int = 0) -> None:
        super().__init__(base_url="unix:///nonexistent/docker.sock", version="1.43")
        self.calls: list[tuple[Any, ...]] = []
        self.created: dict[str, dict[str, Any]] = {}
        self._browser_exit = browser_exit
        self._browser_stdout = json.dumps(RESULT).encode() if browser_stdout is None else browser_stdout
        self._exec_fails = exec_fails

    def inspect_image(self, image: str) -> dict:
        return {"Id": image}

    def create_network(self, name: str, **kwargs: Any) -> dict:
        self.calls.append(("create_network", name, kwargs))
        return {"Id": name}

    def connect_container_to_network(self, container: str, net_id: str, **kwargs: Any) -> None:
        self.calls.append(("connect", net_id, container, kwargs))

    def remove_network(self, net_id: str) -> None:
        self.calls.append(("remove_network", net_id))

    def create_container(self, image: str, command: Any = None, **kwargs: Any) -> dict:
        name = kwargs["name"]
        # The Engine API body docker-py would send, built by docker-py itself.
        body = self.create_container_config(image, command, **{key: value for key, value in kwargs.items() if key != "name"})
        self.created[name] = {"image": image, "command": command, "body": body, **kwargs}
        self.calls.append(("create_container", name))
        return {"Id": name}

    def attach_socket(self, container: str, params: dict | None = None, ws: bool = False) -> _Socket:
        self.calls.append(("attach", container, dict(params or {})))
        return _Socket(self, _frames(self._browser_stdout, b"browser log\n"))

    def start(self, container: str) -> None:
        self.calls.append(("start", container))

    def wait(self, container: str, timeout: float | None = None, condition: str | None = None) -> dict:
        self.calls.append(("wait", container))
        return {"StatusCode": self._browser_exit}

    def remove_container(self, container: str, **kwargs: Any) -> None:
        self.calls.append(("remove_container", container, kwargs))

    def stop(self, container: str, timeout: int | None = None) -> None:
        self.calls.append(("stop", container, timeout))

    def exec_create(self, container: str, cmd: Any, **kwargs: Any) -> dict:
        self.calls.append(("exec", container, cmd[:2]))
        return {"Id": f"exec-{len(self.calls)}"}

    def exec_start(self, exec_id: str, **kwargs: Any) -> bytes:
        return b""

    def exec_inspect(self, exec_id: str) -> dict:
        if self._exec_fails > 0:
            self._exec_fails -= 1
            return {"ExitCode": 1}
        return {"ExitCode": 0}


class _Client:
    def __init__(self, api: _API) -> None:
        self.api = api


@pytest.fixture
def sdk_only(monkeypatch: pytest.MonkeyPatch):
    """No docker CLI at all: any subprocess call for docker fails as it would on an SDK-only runner."""
    monkeypatch.setenv("AETHER_THREAT_BROWSER_IMAGE", BROWSER)
    monkeypatch.setenv("AETHER_TOR_IMAGE", TOR)
    monkeypatch.delenv("AETHER_PROCESS_ROLE", raising=False)

    def no_cli(argv, *_args, **_kwargs):
        if argv and argv[0] == "docker":
            raise FileNotFoundError(2, "No such file or directory", "docker")
        raise AssertionError(f"unexpected subprocess {argv!r}")

    monkeypatch.setattr(subprocess, "run", no_cli)
    monkeypatch.setattr(subprocess, "Popen", no_cli)
    monkeypatch.setattr(threat_launch.time, "sleep", lambda _s: None)

    def install(api: _API) -> _API:
        container_runtime.set_docker_client(_Client(api))
        return api

    yield install
    container_runtime.set_docker_client(None)


def _visit(tor: bool) -> tuple[int, dict]:
    payload = {"url": "https://example.com/note", "browser_enabled": True, "tor_enabled": tor, "proxy": None}
    return runner_process._threat_browser({"payload": payload, "browser_image": BROWSER, "tor_image": TOR})


def _host(api: _API, name: str) -> dict[str, Any]:
    return api.created[name]["host_config"]


def test_direct_visit_runs_on_the_sdk_with_every_flag_mapped(sdk_only) -> None:
    api = sdk_only(_API())
    status, body = _visit(tor=False)
    assert (status, body) == (200, RESULT)
    browsers = [name for name in api.created if name.endswith("-browser")]
    assert len(browsers) == 1
    browser = browsers[0]
    name = browser.removesuffix("-browser")
    created = api.created[browser]
    host = _host(api, browser)
    # docker run --rm -i --name <name>-browser --network <name>-egress ...
    assert ("create_network", f"{name}-egress", {"driver": "bridge", "internal": False, "check_duplicate": True}) in api.calls
    assert created["image"] == BROWSER
    assert created["command"] == ["python3", "-m", ENTRYPOINT]
    assert created["stdin_open"] is True and created["tty"] is False
    assert created["body"]["OpenStdin"] is True and created["body"]["StdinOnce"] is True
    assert created["body"]["AttachStdin"] is True and created["body"]["Tty"] is False
    assert created["user"] == "pwuser"
    assert created["labels"] == {"aether.role": "threat-browser"}
    env = dict(item.split("=", 1) for item in created["environment"])
    assert env["ALL_PROXY"] == ""
    assert {key: env[key] for key in CLEARED} == dict.fromkeys(CLEARED, "")
    assert env["HOME"] == "/tmp" and env["AETHER_BROWSER_NO_SANDBOX"] == "1"
    assert host["NetworkMode"] == f"{name}-egress"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["PidsLimit"] == 128
    assert host["Memory"] == 1073741824 and host["MemorySwap"] == 1073741824
    assert host["ShmSize"] == 268435456
    assert host["Tmpfs"] == {"/tmp": "rw,nosuid,nodev,mode=1777,size=268435456"}
    assert "AutoRemove" not in host or host["AutoRemove"] is False  # removed explicitly after its output is read
    # The visit payload went in on stdin and stdin was closed; then the container was removed (--rm).
    stdin = next(call for call in api.calls if call[0] == "stdin_closed")
    assert json.loads(stdin[1]) == {"url": "https://example.com/note", "browser_enabled": True, "tor_enabled": False, "proxy": None}
    assert ("remove_container", browser, {"force": True}) in api.calls
    # Cleanup: docker network rm <name>-egress.
    assert api.calls[-1] == ("remove_network", f"{name}-egress")


def test_tor_visit_maps_the_internal_network_ip_alias_and_one_shot_tor(sdk_only) -> None:
    api = sdk_only(_API(exec_fails=2))
    status, body = _visit(tor=True)
    assert (status, body) == (200, RESULT)
    tor_name = next(name for name in api.created if name.endswith("-tor"))
    name = tor_name.removesuffix("-tor")
    subnet, gateway, address = link_binding(name)
    link = next(call for call in api.calls if call[:2] == ("create_network", f"{name}-link"))
    assert link[2]["internal"] is True and link[2]["driver"] == "bridge"
    assert link[2]["ipam"]["Config"] == [{"Subnet": subnet, "Gateway": gateway}]
    tor = api.created[tor_name]
    host = _host(api, tor_name)
    assert tor["image"] == TOR and tor["command"] is None and tor["detach"] is True
    assert host["AutoRemove"] is True  # -d --rm
    assert host["NetworkMode"] == f"{name}-link"
    assert tor["networking_config"]["EndpointsConfig"][f"{name}-link"]["IPAMConfig"] == {"IPv4Address": address}
    assert tor["networking_config"]["EndpointsConfig"][f"{name}-link"]["Aliases"] == [TOR_ALIAS]
    assert host["CapDrop"] == ["ALL"] and host["SecurityOpt"] == ["no-new-privileges"]
    assert host["PidsLimit"] == 64 and host["Memory"] == 268435456 and host["MemorySwap"] == 268435456
    assert tor["labels"] == {"aether.role": "tor"}
    env = dict(item.split("=", 1) for item in tor["environment"])
    assert env == {"AETHER_TOR_SOCKS_ADDRESS": address, "AETHER_TOR_SOCKS_POLICY": subnet}
    # docker network connect <name>-egress <name>-tor, then readiness probes until one succeeds.
    assert ("connect", f"{name}-egress", tor_name, {}) in api.calls
    assert [call for call in api.calls if call[0] == "exec"] == [("exec", tor_name, ["python3", "-c"])] * 3
    browser = f"{name}-browser"
    assert _host(api, browser)["NetworkMode"] == f"{name}-link"
    assert dict(item.split("=", 1) for item in api.created[browser]["environment"])["ALL_PROXY"] == APPROVED_TOR_PROXY
    # Cleanup in order: stop tor (it removes itself), remove link, remove egress.
    tail = [call for call in api.calls if call[0] in {"stop", "remove_network"}]
    assert tail == [("stop", tor_name, 10), ("remove_network", f"{name}-link"), ("remove_network", f"{name}-egress")]


def test_a_failed_browser_reports_its_error_and_still_cleans_up(sdk_only) -> None:
    api = sdk_only(_API(browser_exit=1, browser_stdout=b'{"error": "download_rejected"}\n'))
    status, body = _visit(tor=False)
    assert (status, body) == (200, {"error": "download_rejected"})
    assert any(call[0] == "remove_container" for call in api.calls)
    assert api.calls[-1][0] == "remove_network"


def test_no_daemon_is_an_explicit_docker_unavailable(sdk_only, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable() -> Any:
        raise DockerException("Error while fetching server API version")

    monkeypatch.setattr(container_runtime, "docker_client", unreachable)
    assert _visit(tor=False) == (200, {"error": "docker_unavailable"})


def test_cleanup_tolerates_what_is_already_gone(sdk_only) -> None:
    class _Gone(_API):
        def remove_network(self, net_id: str) -> None:
            raise NotFound("gone")

        def stop(self, container: str, timeout: int | None = None) -> None:
            raise NotFound("gone")

    sdk_only(_Gone())
    assert _visit(tor=True) == (200, RESULT)


def test_the_worker_still_cannot_reach_docker(sdk_only, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk_only(_API())
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    with pytest.raises(threat_launch.ThreatLaunchError) as runner_refused:
        threat_launch.docker_runner(["network", "create", "x"], b"")
    with pytest.raises(threat_launch.ThreatLaunchError) as cleanup_refused:
        threat_launch.docker_cleanup(["network", "rm", "x"], b"")
    assert runner_refused.value.code == cleanup_refused.value.code == "docker_forbidden"
