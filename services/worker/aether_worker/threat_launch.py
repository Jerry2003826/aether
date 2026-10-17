"""One-shot browser container. Docker runs in the runner process, not the worker.

The launch is written as docker CLI argument lists (setup_commands, browser_command, cleanup_commands)
and run through the Docker SDK (docker_runner / docker_cleanup), like every other runner Docker call:
a runner with the SDK and a socket but no docker CLI can run it. Each flag maps 1:1 onto the Engine
API field it stands for (see _sdk_run); a flag or command outside that set is refused, never guessed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import time
import uuid
from collections.abc import Callable
from typing import Any

import requests
from docker.errors import DockerException, ImageNotFound, NotFound

from aether_worker import container_runtime
from aether_worker.loopback import LoopbackError, post_json
from aether_worker.output_bounds import RESULT_CAPTURE_LIMIT
from aether_worker.threat_pins import (
    APPROVED_TOR_PROXY,
    ENTRYPOINT,
    TOR_ALIAS,
    TOR_SOCKS_PORT,
)

Runner = Callable[[list[str], bytes], bytes]
_MEMORY_TOR = "268435456"
_MEMORY_BROWSER = "1073741824"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CLEARED = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
_FIXTURE_PORT = 8080
_READY_ATTEMPTS = 30
_READY_PAUSE = 0.25
_LINK_SUPERNET = ipaddress.ip_network("10.240.0.0/16")


class ThreatLaunchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require_digest(image: str) -> str:
    token = image.split("@", 1)[1] if "@sha256:" in image else image
    if _DIGEST.fullmatch(token) is None:
        raise ThreatLaunchError("image_unpinned")
    return image


def container_name() -> str:
    return f"aether-tb-{uuid.uuid4().hex[:8]}"


def link_binding(name: str) -> tuple[str, str, str]:
    """Return the internal subnet, gateway, and address SOCKS may bind.

    The visit name selects one /28 inside 10.240.0.0/16. The Tor container
    receives that address on the internal network only.
    """
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,48}", name) is None:
        raise ThreatLaunchError("container_name_rejected")
    block = int.from_bytes(hashlib.sha256(name.encode("ascii")).digest()[:2], "big") % 4096
    third = block // 16
    fourth = (block % 16) * 16
    network = ipaddress.ip_network(f"10.240.{third}.{fourth}/28")
    if not network.subnet_of(_LINK_SUPERNET):
        raise ThreatLaunchError("container_name_rejected")
    hosts = list(network.hosts())
    return str(network), str(hosts[0]), str(hosts[1])


def setup_commands(name: str, *, tor_image: str, fixture: bool = False, tor_enabled: bool = False) -> list[list[str]]:
    egress = f"{name}-egress"
    if not tor_enabled and not fixture:
        return [["network", "create", egress]]
    link = f"{name}-link"
    tor_name = f"{name}-tor"
    subnet, gateway, address = link_binding(name)
    return [
        ["network", "create", "--internal", "--subnet", subnet, "--gateway", gateway, link],
        ["network", "create", egress],
        _tor_command(tor_name, link, require_digest(tor_image), fixture=fixture, address=address, subnet=subnet),
        ["network", "connect", egress, tor_name],
    ]


def browser_command(
    name: str,
    *,
    image: str,
    use_tor_proxy: bool,
    fixture: bool = False,
    command: tuple[str, ...] = ("python3", "-m", ENTRYPOINT),
    extra_env: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    args = [
        "run",
        "--rm",
        "-i",
        "--name",
        f"{name}-browser",
        "--network",
        f"{name}-link" if use_tor_proxy or fixture else f"{name}-egress",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        "--memory",
        _MEMORY_BROWSER,
        "--memory-swap",
        _MEMORY_BROWSER,
        "--shm-size",
        "268435456",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,mode=1777,size=268435456",
        "--user",
        "pwuser",
        "-e",
        "HOME=/tmp",
        "-e",
        "XDG_CACHE_HOME=/tmp",
        "-e",
        "XDG_CONFIG_HOME=/tmp",
        "-e",
        "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright",
        "-e",
        "AETHER_BROWSER_NO_SANDBOX=1",
        "-e",
        f"ALL_PROXY={APPROVED_TOR_PROXY if use_tor_proxy else ''}",
    ]
    for key in _CLEARED:
        args.extend(["-e", f"{key}="])
    if fixture:
        args.extend(["-e", "AETHER_INGEST_ALLOW_FIXTURES=1"])
    for key, value in extra_env:
        args.extend(["-e", f"{key}={value}"])
    args.extend(
        [
            "--label",
            "aether.role=threat-browser",
            require_digest(image),
            *command,
        ]
    )
    return args


def cleanup_commands(name: str, *, tor: bool) -> list[list[str]]:
    commands: list[list[str]] = []
    if tor:
        commands.append(["stop", f"{name}-tor"])
        commands.append(["network", "rm", f"{name}-link"])
    commands.append(["network", "rm", f"{name}-egress"])
    return commands


def execute_container(
    payload: dict[str, object],
    *,
    runner: Runner,
    cleanup: Runner,
    browser_image: str,
    tor_image: str,
    fixture: bool = False,
) -> dict[str, object]:
    """Run setup, one browser container, then always cleanup. No local browser fallback."""
    browser_image = require_digest(browser_image)
    tor_image = require_digest(tor_image)
    name = container_name()
    tor_on = payload.get("tor_enabled") is True or fixture
    try:
        for command in setup_commands(name, tor_image=tor_image, fixture=fixture, tor_enabled=payload.get("tor_enabled") is True):
            runner(command, b"")
        if tor_on:
            _wait_for_proxy(name, runner, fixture=fixture)
        raw = runner(
            browser_command(
                name,
                image=browser_image,
                use_tor_proxy=payload.get("tor_enabled") is True,
                fixture=fixture,
            ),
            json.dumps(payload).encode(),
        )
    finally:
        for command in cleanup_commands(name, tor=tor_on):
            _cleanup(cleanup, command)
    return _object(raw)


def remote_execute(
    payload: dict[str, object],
    *,
    browser_image: str,
    tor_image: str,
    fixture: bool,
) -> dict[str, object]:
    """Ask the runner process to build and run the containers."""
    base = os.environ.get("AETHER_RUNNER_BASE", "").rstrip("/")
    token = os.environ.get("AETHER_LOOPBACK_TOKEN", "")
    if not base or not token:
        raise ThreatLaunchError("runner_unavailable")
    try:
        body = post_json(
            f"{base}/v1/threat-browser",
            token,
            {"payload": payload, "browser_image": browser_image, "tor_image": tor_image, "fixture": fixture},
            timeout=180,
        )
    except LoopbackError as exc:
        raise ThreatLaunchError("runner_unavailable") from exc
    code = body.get("error")
    if isinstance(code, str) and code != "":
        raise ThreatLaunchError(code)
    return body


def docker_runner(args: list[str], stdin: bytes) -> bytes:
    """Run one launch command through the Docker SDK; its stdout, or ThreatLaunchError.

    docker_unavailable: the SDK cannot reach a daemon. A failed command is the error code its
    stdout reports (the browser's JSON), else container_unavailable, as with the CLI before.
    """
    _reject_worker_docker()
    return _sdk(args, stdin)


def docker_cleanup(args: list[str], stdin: bytes) -> bytes:
    """Best effort, like `docker ... || true`: what is already gone, or any failure, is ignored."""
    _reject_worker_docker()
    try:
        return _sdk(args, stdin)
    except ThreatLaunchError:
        return b""


# Seconds a browser run may take before it is killed; the worker's request to the runner allows 180.
_BROWSER_RUN_SECONDS = 170.0
_STOP_GRACE_SECONDS = 10  # `docker stop` default


def _sdk(args: list[str], stdin: bytes) -> bytes:
    try:
        api = container_runtime.docker_client().api
    except (DockerException, requests.exceptions.RequestException) as exc:
        raise ThreatLaunchError("docker_unavailable") from exc
    try:
        if args[:2] == ["network", "create"]:
            return _sdk_network_create(api, args[2:])
        if args[:2] == ["network", "connect"] and len(args) == 4:
            api.connect_container_to_network(args[3], args[2])
            return b""
        if args[:2] == ["network", "rm"] and len(args) == 3:
            api.remove_network(args[2])
            return b""
        if args[:1] == ["stop"] and len(args) == 2:
            api.stop(args[1], timeout=_STOP_GRACE_SECONDS)
            return b""
        if args[:1] == ["exec"] and len(args) >= 3 and not args[1].startswith("-"):
            return _sdk_exec(api, args[1], args[2:])
        if args[:1] == ["run"]:
            return _sdk_run(api, args[1:], stdin)
    except ThreatLaunchError:
        raise
    except NotFound as exc:
        raise ThreatLaunchError("container_unavailable") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ThreatLaunchError("docker_unavailable") from exc
    except (DockerException, requests.exceptions.RequestException, OSError) as exc:
        raise ThreatLaunchError("container_unavailable") from exc
    raise ThreatLaunchError("docker_args_unsupported")


def _sdk_network_create(api: Any, args: list[str]) -> bytes:
    """`network create [--internal] [--subnet S --gateway G] NAME` -> a bridge network, as the CLI makes."""
    internal = False
    subnet = gateway = None
    index = 0
    while index < len(args) - 1:
        token = args[index]
        if token == "--internal":
            internal = True
        elif token in {"--subnet", "--gateway"}:
            index += 1
            if token == "--subnet":
                subnet = args[index]
            else:
                gateway = args[index]
        else:
            raise ThreatLaunchError("docker_args_unsupported")
        index += 1
    if index != len(args) - 1 or args[-1].startswith("-") or (gateway is not None and subnet is None):
        raise ThreatLaunchError("docker_args_unsupported")
    kwargs: dict[str, Any] = {"driver": "bridge", "internal": internal, "check_duplicate": True}
    if subnet is not None:
        pool = {"Subnet": subnet} if gateway is None else {"Subnet": subnet, "Gateway": gateway}
        kwargs["ipam"] = {"Driver": "default", "Config": [pool], "Options": {}}
    api.create_network(args[-1], **kwargs)
    return b""


def _sdk_exec(api: Any, name: str, command: list[str]) -> bytes:
    """`exec NAME CMD...`: stdout only (as the runner reads it); a non-zero exit is a failure."""
    created = api.exec_create(name, command, stdout=True, stderr=False)
    output = api.exec_start(created["Id"])
    output = output if isinstance(output, bytes) else b""
    if int(api.exec_inspect(created["Id"]).get("ExitCode") or 0) != 0:
        raise ThreatLaunchError(_error_code(output))
    return output


_RUN_VALUE_FLAGS = {
    "--name",
    "--network",
    "--ip",
    "--network-alias",
    "--cap-drop",
    "--security-opt",
    "--pids-limit",
    "--memory",
    "--memory-swap",
    "--shm-size",
    "--tmpfs",
    "--user",
    "-e",
    "--label",
}


def _sdk_run(api: Any, args: list[str], stdin: bytes) -> bytes:
    """`docker run` for the flags threat_launch writes, each onto its Engine API field:

    -d -> detached (returns the id); --rm -> AutoRemove when detached, else removed (force) once its
    output is read; -i -> stdin_open (+ StdinOnce) with `stdin` written and closed; --name; --network
    -> NetworkMode; --ip / --network-alias -> that network's endpoint IPv4Address / Aliases;
    --read-only -> ReadonlyRootfs; --cap-drop -> CapDrop; --security-opt -> SecurityOpt;
    --pids-limit / --memory / --memory-swap / --shm-size -> PidsLimit / Memory / MemorySwap / ShmSize;
    --tmpfs PATH:OPTS -> Tmpfs; --user; -e K=V -> Env (in order); --label K=V -> Labels.
    """
    detach = remove = interactive = read_only = False
    values: dict[str, list[str]] = {flag: [] for flag in _RUN_VALUE_FLAGS}
    index = 0
    while index < len(args) and args[index].startswith("-"):
        token = args[index]
        if token == "-d":
            detach = True
        elif token == "--rm":
            remove = True
        elif token == "-i":
            interactive = True
        elif token == "--read-only":
            read_only = True
        elif token in _RUN_VALUE_FLAGS and index + 1 < len(args):
            index += 1
            values[token].append(args[index])
        else:
            raise ThreatLaunchError("docker_args_unsupported")
        index += 1
    if index >= len(args) or detach == interactive:
        # One image, and exactly one of -d (Tor) or -i (the browser): the two forms this module writes.
        raise ThreatLaunchError("docker_args_unsupported")
    image, command = args[index], args[index + 1 :]
    single = {flag: (items[-1] if items else None) for flag, items in values.items()}
    network = single["--network"]
    if network is None or single["--name"] is None:
        raise ThreatLaunchError("docker_args_unsupported")
    tmpfs: dict[str, str] = {}
    for spec in values["--tmpfs"]:
        path, _, options = spec.partition(":")
        tmpfs[path] = options
    labels: dict[str, str] = {}
    for spec in values["--label"]:
        key, _, value = spec.partition("=")
        labels[key] = value
    if any("=" not in item for item in values["-e"]):
        raise ThreatLaunchError("docker_args_unsupported")
    host_config = api.create_host_config(
        network_mode=network,
        auto_remove=remove and detach,
        read_only=read_only or None,
        cap_drop=values["--cap-drop"] or None,
        security_opt=values["--security-opt"] or None,
        pids_limit=None if single["--pids-limit"] is None else int(single["--pids-limit"]),
        mem_limit=None if single["--memory"] is None else int(single["--memory"]),
        memswap_limit=None if single["--memory-swap"] is None else int(single["--memory-swap"]),
        shm_size=None if single["--shm-size"] is None else int(single["--shm-size"]),
        tmpfs=tmpfs or None,
    )
    networking_config = None
    if single["--ip"] is not None or values["--network-alias"]:
        endpoint = api.create_endpoint_config(ipv4_address=single["--ip"], aliases=values["--network-alias"] or None)
        networking_config = api.create_networking_config({network: endpoint})
    _sdk_image(api, image)
    created = api.create_container(
        image,
        command or None,
        name=single["--name"],
        user=single["--user"],
        environment=values["-e"] or None,
        labels=labels or None,
        host_config=host_config,
        networking_config=networking_config,
        detach=detach,
        stdin_open=interactive,
        tty=False,
    )
    container = created["Id"]
    if detach:
        try:
            api.start(container)
        except BaseException:
            _sdk_remove(api, container)
            raise
        return (str(container) + "\n").encode()
    try:
        return _sdk_attached(api, container, stdin)
    finally:
        if remove:
            _sdk_remove(api, container)


def _sdk_image(api: Any, image: str) -> None:
    """A missing image is pulled (as `docker run` would) only when it names a registry digest."""
    try:
        api.inspect_image(image)
        return
    except ImageNotFound:
        if "@sha256:" not in image:
            raise ThreatLaunchError("image_unavailable") from None
    api.pull(image)


def _sdk_remove(api: Any, container: str) -> None:
    try:
        api.remove_container(container, force=True)
    except (DockerException, requests.exceptions.RequestException):
        pass


def _sdk_attached(api: Any, container: str, stdin: bytes) -> bytes:
    """`run -i` without -d: attach first, start, write stdin and close it, read stdout to the end."""
    attached = api.attach_socket(container, params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
    raw = getattr(attached, "_sock", attached)
    try:
        raw.settimeout(_BROWSER_RUN_SECONDS)
        api.start(container)
        raw.sendall(stdin)
        raw.shutdown(socket.SHUT_WR)
        stdout = _read_frames(raw)
    except TimeoutError as exc:
        raise ThreatLaunchError("container_unavailable") from exc
    finally:
        try:
            raw.close()
        except OSError:
            pass
    status = api.wait(container, timeout=_BROWSER_RUN_SECONDS)
    if int((status or {}).get("StatusCode", 1)) != 0:
        raise ThreatLaunchError(_error_code(stdout))
    return stdout


def _read_frames(raw: Any) -> bytes:
    """Demultiplex a non-tty attach stream (8-byte header: stream, 0, 0, 0, big-endian length).

    stdout is the result and is bounded like any runner result; stderr (the browser's log, which
    the CLI path never read either) is discarded. A result over its bound is refused rather than cut.
    """
    stdout = bytearray()
    buffer = b""
    while True:
        chunk = raw.recv(65536)
        if not chunk:
            break
        buffer += chunk
        while len(buffer) >= 8:
            stream, size = struct.unpack(">BxxxL", buffer[:8])
            if len(buffer) < 8 + size:
                break
            data, buffer = buffer[8 : 8 + size], buffer[8 + size :]
            if stream == 1:
                stdout += data
                if len(stdout) > RESULT_CAPTURE_LIMIT:
                    raise ThreatLaunchError("container_unavailable")
    return bytes(stdout)


def _reject_worker_docker() -> None:
    if os.environ.get("AETHER_PROCESS_ROLE") == "worker":
        raise ThreatLaunchError("docker_forbidden")


def _tor_command(tor_name: str, link: str, image: str, *, fixture: bool, address: str, subnet: str) -> list[str]:
    command = [
        "run",
        "-d",
        "--rm",
        "--name",
        tor_name,
        "--network",
        link,
        "--ip",
        address,
        "--network-alias",
        TOR_ALIAS,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        _MEMORY_TOR,
        "--memory-swap",
        _MEMORY_TOR,
        "--label",
        "aether.role=tor",
        "-e",
        f"AETHER_TOR_SOCKS_ADDRESS={address}",
        "-e",
        f"AETHER_TOR_SOCKS_POLICY={subnet}",
    ]
    if fixture:
        command.extend(["-e", "AETHER_TOR_DISABLE_NETWORK=1", "-e", "AETHER_TOR_FIXTURE=1"])
    command.append(image)
    return command


def _wait_for_proxy(name: str, runner: Runner, *, fixture: bool) -> None:
    """Probe the Tor container before the browser starts.

    DisableNetwork refuses SOCKS, so the fixture probe is the page port on the
    internal address. A real Tor visit waits until that same address completes
    a SOCKS greeting. Loopback is not used, because SOCKS is not bound there.
    """
    script = _ready_script(fixture)
    for _attempt in range(_READY_ATTEMPTS):
        try:
            runner(["exec", f"{name}-tor", "python3", "-c", script], b"")
        except ThreatLaunchError:
            time.sleep(_READY_PAUSE)
            continue
        return
    raise ThreatLaunchError("tor_not_ready")


def _ready_script(fixture: bool) -> str:
    host = "host=os.environ['AETHER_TOR_SOCKS_ADDRESS']\n"
    if fixture:
        return (
            "import os,socket\n"
            + host
            + f"socket.create_connection((host,{_FIXTURE_PORT}),2).close()\n"
        )
    return (
        "import os,socket,sys\n"
        + host
        + f"sock=socket.create_connection((host,{TOR_SOCKS_PORT}),2)\n"
        "sock.sendall(b'\\x05\\x01\\x00')\n"
        "reply=sock.recv(2)\n"
        "sock.close()\n"
        "sys.exit(0 if reply==b'\\x05\\x00' else 1)\n"
    )


def _cleanup(runner: Runner, command: list[str]) -> None:
    try:
        runner(command, b"")
    except Exception:
        return


def _object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ThreatLaunchError("container_unavailable") from exc
    if not isinstance(value, dict):
        raise ThreatLaunchError("container_unavailable")
    code = value.get("error")
    if isinstance(code, str) and code != "":
        raise ThreatLaunchError(code)
    return value


def _error_code(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode())
    except (UnicodeError, json.JSONDecodeError):
        return "container_unavailable"
    if isinstance(value, dict) and isinstance(value.get("error"), str) and value["error"] != "":
        return str(value["error"])
    return "container_unavailable"
