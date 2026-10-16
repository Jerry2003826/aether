"""Build and run the one-shot browser and Tor containers. Missing Docker fails closed."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from aether_worker.threat_launch import browser_command, cleanup_commands, container_name, setup_commands
from aether_worker.threat_pins import CHROMIUM_BROWSER_VERSION, PLAYWRIGHT_VERSION, PROBE
from aether_worker.threat_probe import blocked

ROOT = Path(__file__).resolve().parents[2]


def test_one_shot_containers_only_reach_tor_and_are_removed() -> None:
    _require_docker()
    browser = _build("infra/threat-browser.Dockerfile")
    tor = _build("infra/tor.Dockerfile")
    name = container_name()
    decoy = f"{name}-decoy"
    try:
        _setup(name, tor)
        _docker(
            ["run", "-d", "--rm", "--name", decoy, "--network", f"{name}-egress", "--entrypoint", "python3", tor, "-m", "http.server", "8080", "--bind", "0.0.0.0"],
            check=True,
        )
        egress = _text(["inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", decoy])
        report = _wait_for_tor(name, browser, f"{egress}:8080")
        certificate = _certificate(name)
        assert report["default_route"] is False
        assert report["tor_socks"] == "refused"
        assert report["fixture"] == "open"
        assert _cmdline(name).startswith("tor ")
        assert report["uid"] != 0
        for key in ("public", "private", "lan", "metadata", "egress"):
            assert blocked(str(report[key]))
        assert _text(["exec", f"{name}-tor", "id", "-u"]) != "0"
        assert "DisableNetwork 1" in _text(["exec", f"{name}-tor", "cat", "/var/lib/tor/aether-torrc"])
        page = _visit(name, browser, "/page", certificate)
        assert page["playwright_version"] == PLAYWRIGHT_VERSION
        assert page["browser_version"] == CHROMIUM_BROWSER_VERSION
        assert "HeadlessChrome" in str(page["user_agent"])
        assert "visible-text" in str(page["text"])
        assert "noscript-text" in str(page["text"])
        assert "RAN" not in str(page["text"])
        assert "hidden-instruction" not in str(page["text"])
        _absent(f"{name}-browser")
        hits = json.loads(_text(["exec", f"{name}-tor", "cat", "/var/lib/tor/fixture/hits.json"]))
        assert hits["page"] == 1
        assert hits["pixel"] == 0
        rejected = _visit(name, browser, "/payload.exe", certificate)
        assert rejected["error"] == "download_rejected"
        _absent(f"{name}-browser")
    finally:
        _docker(["stop", decoy])
        for command in cleanup_commands(name, tor=True):
            _docker(command)
    _absent(f"{name}-tor")
    _absent(decoy)
    _absent_network(f"{name}-link")
    _absent_network(f"{name}-egress")


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required when AETHER_REQUIRE_SERVICES=1; the container test does not skip")
    info = subprocess.run(["docker", "info"], capture_output=True, check=False)
    if info.returncode != 0:
        pytest.fail("Docker daemon is required when AETHER_REQUIRE_SERVICES=1; the container test does not skip")


def _build(dockerfile: str) -> str:
    tag = f"aether-tb-build-{Path(dockerfile).stem}"
    completed = subprocess.run(
        ["docker", "build", "--progress=plain", "-t", tag, "-f", dockerfile, "."],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        log = (completed.stdout + completed.stderr).decode(errors="replace")
        pytest.fail(log[-8000:])
    image = _text(["image", "inspect", "-f", "{{.Id}}", tag])
    if not image.startswith("sha256:"):
        pytest.fail(image or "docker build did not return an image id")
    return image


def _setup(name: str, tor: str) -> None:
    for command in setup_commands(name, tor_image=tor, fixture=True):
        _docker(command, check=True)


def _wait_for_tor(name: str, browser: str, egress: str) -> dict[str, object]:
    last: dict[str, object] = {}
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        last = _probe(name, browser, egress)
        if last.get("fixture") == "open" and last.get("tor_socks") == "refused" and _cmdline(name).startswith("tor "):
            return last
        time.sleep(0.5)
    pytest.fail(f"tor fixture did not open: {last} cmd={_cmdline(name)}")


def _cmdline(name: str) -> str:
    completed = _docker(["exec", f"{name}-tor", "cat", "/proc/1/cmdline"])
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode(errors="replace").replace("\x00", " ")


def _probe(name: str, browser: str, egress: str) -> dict[str, object]:
    command = browser_command(
        name,
        image=browser,
        use_tor_proxy=False,
        fixture=True,
        command=("python3", "-m", PROBE),
        extra_env=(("AETHER_PROBE_EGRESS", egress),),
    )
    completed = _docker(command)
    _absent(f"{name}-browser")
    if completed.returncode != 0:
        return {"error": completed.stderr.decode(errors="replace")[-1000:]}
    return json.loads(completed.stdout.decode())


def _certificate(name: str) -> str:
    completed = _docker(["exec", f"{name}-tor", "cat", "/var/lib/tor/fixture/cert.pem"], check=True)
    return base64.b64encode(completed.stdout).decode()


def _visit(name: str, browser: str, path: str, certificate: str) -> dict[str, object]:
    command = browser_command(
        name,
        image=browser,
        use_tor_proxy=False,
        fixture=True,
        extra_env=(("AETHER_FIXTURE_CA", certificate),),
    )
    payload = {
        "url": f"https://aether-tor:8080{path}",
        "browser_enabled": True,
        "tor_enabled": False,
        "onion_enabled": False,
        "owner_confirmed": False,
        "allow_hosts": ["aether-tor"],
        "proxy": None,
    }
    completed = _docker(command, stdin=json.dumps(payload).encode(), check=False)
    stdout = completed.stdout.decode(errors="replace").strip()
    stderr = completed.stderr.decode(errors="replace")[-2000:]
    if stdout == "":
        pytest.fail(stderr or "browser container produced no output")
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        pytest.fail(f"{stdout[-2000:]}\n{stderr}")
    if not isinstance(body, dict):
        pytest.fail(stdout[-2000:])
    if completed.returncode != 0 and body.get("error") != "download_rejected":
        pytest.fail(f"{body}\n{stderr}")
    return body


def _docker(args: list[str], *, stdin: bytes = b"", check: bool = False) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(["docker", *args], input=stdin, capture_output=True, check=False)
    if check and completed.returncode != 0:
        pytest.fail(completed.stderr.decode(errors="replace")[-4000:])
    return completed


def _text(args: list[str]) -> str:
    completed = _docker(args, check=True)
    return completed.stdout.decode().strip()


def _absent(name: str) -> None:
    completed = _docker(["inspect", name])
    assert completed.returncode != 0


def _absent_network(name: str) -> None:
    completed = _docker(["network", "inspect", name])
    assert completed.returncode != 0
