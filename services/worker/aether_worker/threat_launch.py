"""One-shot browser container. Docker runs in the runner process, not the worker."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable

from aether_worker.loopback import LoopbackError, post_json
from aether_worker.threat_pins import APPROVED_TOR_PROXY, ENTRYPOINT, TOR_ALIAS, TOR_SOCKS_PORT

Runner = Callable[[list[str], bytes], bytes]
_MEMORY_TOR = "268435456"
_MEMORY_BROWSER = "1073741824"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CLEARED = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
_FIXTURE_PORT = 8080
_READY_ATTEMPTS = 30
_READY_PAUSE = 0.25


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


def setup_commands(name: str, *, tor_image: str, fixture: bool = False, tor_enabled: bool = False) -> list[list[str]]:
    egress = f"{name}-egress"
    if not tor_enabled and not fixture:
        return [["network", "create", egress]]
    link = f"{name}-link"
    tor_name = f"{name}-tor"
    return [
        ["network", "create", "--internal", link],
        ["network", "create", egress],
        _tor_command(tor_name, link, require_digest(tor_image), fixture=fixture),
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
    _reject_worker_docker()
    completed = subprocess.run(["docker", *args], input=stdin, capture_output=True, check=False)
    if completed.returncode != 0:
        raise ThreatLaunchError(_error_code(completed.stdout))
    return completed.stdout


def docker_cleanup(args: list[str], stdin: bytes) -> bytes:
    _reject_worker_docker()
    completed = subprocess.run(["docker", *args], input=stdin, capture_output=True, check=False)
    return completed.stdout


def _reject_worker_docker() -> None:
    if os.environ.get("AETHER_PROCESS_ROLE") == "worker":
        raise ThreatLaunchError("docker_forbidden")


def _tor_command(tor_name: str, link: str, image: str, *, fixture: bool) -> list[str]:
    command = [
        "run",
        "-d",
        "--rm",
        "--name",
        tor_name,
        "--network",
        link,
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
    ]
    if fixture:
        command.extend(["-e", "AETHER_TOR_DISABLE_NETWORK=1", "-e", "AETHER_TOR_FIXTURE=1"])
    command.append(image)
    return command


def _wait_for_proxy(name: str, runner: Runner, *, fixture: bool) -> None:
    """Probe the Tor container before the browser starts.

    DisableNetwork refuses SOCKS, so the fixture probe is the local page port.
    A real Tor visit waits until 127.0.0.1:9050 completes a SOCKS greeting.
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
    if fixture:
        return (
            "import socket\n"
            f"socket.create_connection(('127.0.0.1',{_FIXTURE_PORT}),2).close()\n"
        )
    return (
        "import socket,sys\n"
        f"sock=socket.create_connection(('127.0.0.1',{TOR_SOCKS_PORT}),2)\n"
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
