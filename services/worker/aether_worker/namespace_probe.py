"""Run inside a fresh user, mount, pid, and net namespace. Prints one JSON object."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import socket

MS_REC = 16384
MS_PRIVATE = 1 << 18


def _mount(source: bytes | None, target: bytes, fstype: bytes | None, flags: int) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p]
    if libc.mount(source, target, fstype, flags, None) != 0:
        raise OSError(ctypes.get_errno(), "mount")


def main() -> None:
    hide = os.environ["AETHER_HIDE_PATH"]
    _mount(None, b"/", None, MS_REC | MS_PRIVATE)
    _mount(b"tmpfs", hide.encode(), b"tmpfs", 0)
    network_errno = 0
    probe = socket.socket()
    probe.settimeout(1)
    try:
        probe.connect(("1.1.1.1", 443))
    except OSError as exc:
        network_errno = exc.errno or 0
    payload = {
        "pid": os.getpid(),
        "network_errno": network_errno,
        "host_marker_hidden": not os.path.exists(os.path.join(hide, "marker")),
        "provider_key_present": "AETHER_PROVIDER_KEY" in os.environ,
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
