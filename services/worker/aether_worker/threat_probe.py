"""Reachability probe for the one-shot browser container. It never sends a SOCKS connect."""

from __future__ import annotations

import errno
import json
import os
import socket
import sys

_BLOCKED = {"unreachable", "timeout"}


def main() -> None:
    egress = os.environ.get("AETHER_PROBE_EGRESS", "")
    host, port = _endpoint(egress)
    socks = _socks("aether-tor", 9050)
    fixture = _tcp("aether-tor", 8080)
    ready = fixture == "open"
    report: dict[str, object] = {
        "uid": os.getuid(),
        "default_route": _default_route() if ready else False,
        "tor_socks": socks,
        "fixture": fixture,
        "public": _tcp("1.1.1.1", 443) if ready else "pending",
        "private": _tcp("10.255.255.1", 80) if ready else "pending",
        "lan": _tcp("192.168.0.1", 80) if ready else "pending",
        "metadata": _tcp("169.254.169.254", 80) if ready else "pending",
        "egress": _tcp(host, port) if ready else "pending",
    }
    json.dump(report, sys.stdout)


def blocked(state: str) -> bool:
    return state in _BLOCKED


def _endpoint(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if separator != ":" or host == "" or not port.isdigit():
        return "203.0.113.10", 9
    return host, int(port)


def _default_route() -> bool:
    with open("/proc/net/route", encoding="ascii") as handle:
        lines = handle.read().splitlines()[1:]
    return any(line.split()[1] == "00000000" for line in lines if line.strip())


def _tcp(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), 2):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return "timeout"
    except OSError as exc:
        return _os_state(exc)


def _socks(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), 2) as conn:
            conn.settimeout(2)
            conn.sendall(b"\x05\x01\x00")
            reply = conn.recv(2)
    except TimeoutError:
        return "timeout"
    except OSError as exc:
        return _os_state(exc)
    if reply == b"\x05\x00":
        return "open"
    return "rejected"


def _os_state(exc: OSError) -> str:
    if exc.errno == errno.ECONNREFUSED:
        return "refused"
    if exc.errno in {errno.ETIMEDOUT, errno.EAGAIN}:
        return "timeout"
    return "unreachable"


if __name__ == "__main__":
    main()
