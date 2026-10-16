"""Install one signed tor package from deb.torproject.org. Tags are not accepted."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

FINGERPRINT = "A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89"
DISTRIBUTION = "trixie"
VERSION = "0.4.9.14-1~d13.trixie+1"
BASE = "https://deb.torproject.org/torproject.org"
ROOT = Path("/var/lib/aether-tor-build")
GNUPGHOME = ROOT / "gnupg"


def main() -> None:
    global ROOT, GNUPGHOME
    verify_only = "--verify-only" in sys.argv
    ROOT = _choose_root()
    GNUPGHOME = ROOT / "gnupg"
    ROOT.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ROOT, 0o700)
    try:
        _install(verify_only)
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)


def _choose_root() -> Path:
    preferred = Path("/var/lib/aether-tor-build")
    parent = preferred.parent
    if parent.is_dir() and os.access(parent, os.W_OK):
        return preferred
    return Path.cwd() / "aether-tor-build"


def _install(verify_only: bool) -> None:
    GNUPGHOME.mkdir(mode=0o700, exist_ok=True)
    os.chmod(GNUPGHOME, 0o700)
    os.environ["GNUPGHOME"] = str(GNUPGHOME)
    key = ROOT / "archive.asc"
    _fetch(f"{BASE}/{FINGERPRINT}.asc", key)
    _require_fingerprint(key)
    _import(key)
    release = ROOT / "InRelease"
    _fetch(f"{BASE}/dists/{DISTRIBUTION}/InRelease", release)
    body = _verified_body(release)
    arch = _architecture()
    packages = _verified_packages(body, arch)
    filename, digest = _tor_record(packages, arch)
    if verify_only:
        print(f"verified {VERSION} {filename} {digest}")
        return
    _require_policy()
    deb = ROOT / "tor.deb"
    _fetch(f"{BASE}/{filename}", deb)
    if hashlib.sha256(deb.read_bytes()).hexdigest() != digest:
        raise SystemExit("tor deb hash mismatch")
    _run(["apt-get", "update"])
    _run(
        [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "adduser",
            "lsb-base",
            "runit-helper",
            "libcap2",
            "libevent-2.1-7t64",
            "liblzma5",
            "libseccomp2",
            "libssl3t64",
            "libsystemd0",
            "libzstd1",
            "zlib1g",
        ]
    )
    _run(["dpkg", "-i", str(deb)])


def _fetch(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "aether-tor-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        path.write_bytes(response.read())


def _require_fingerprint(key: Path) -> None:
    completed = subprocess.run(
        ["gpg", "--batch", "--show-keys", "--with-colons", str(key)],
        check=True,
        capture_output=True,
        text=True,
    )
    fingerprints = [line.split(":")[9] for line in completed.stdout.splitlines() if line.startswith("fpr:")]
    if not fingerprints or fingerprints[0] != FINGERPRINT:
        raise SystemExit("tor key fingerprint mismatch")


def _import(key: Path) -> None:
    subprocess.run(["gpg", "--batch", "--import", str(key)], check=True, capture_output=True)


def _verified_body(release: Path) -> str:
    completed = subprocess.run(
        ["gpg", "--batch", "--status-fd", "1", "--output", str(ROOT / "release-body"), "--decrypt", str(release)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or "GOODSIG" not in completed.stdout or "VALIDSIG" not in completed.stdout:
        raise SystemExit(completed.stderr or completed.stdout or "tor release signature rejected")
    return (ROOT / "release-body").read_text(encoding="utf-8")


def _architecture() -> str:
    completed = subprocess.run(["dpkg", "--print-architecture"], check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _verified_packages(body: str, arch: str) -> str:
    wanted = f"main/binary-{arch}/Packages"
    digest = _indexed_hash(body, wanted)
    path = ROOT / "Packages"
    _fetch(f"{BASE}/dists/{DISTRIBUTION}/{wanted}", path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit("tor package index hash mismatch")
    return path.read_text(encoding="utf-8")


def _indexed_hash(body: str, wanted: str) -> str:
    section = False
    for line in body.splitlines():
        if line.startswith("SHA256:"):
            section = True
            continue
        if section and not line.startswith(" "):
            section = False
        if section:
            parts = line.split()
            if len(parts) == 3 and parts[2] == wanted:
                return parts[0]
    raise SystemExit("tor package index hash missing")


def _tor_record(packages: str, arch: str) -> tuple[str, str]:
    expected = f"pool/main/t/tor/tor_{VERSION}_{arch}.deb"
    for block in packages.split("\n\n"):
        fields = _fields(block)
        if fields.get("Package") != "tor":
            continue
        if fields.get("Version") != VERSION or fields.get("Filename") != expected:
            raise SystemExit("tor package pin mismatch")
        digest = fields.get("SHA256", "")
        if len(digest) != 64:
            raise SystemExit("tor deb hash missing")
        return expected, digest
    raise SystemExit("tor package missing")


def _fields(block: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in block.splitlines():
        if line.startswith(" ") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        found[key] = value.strip()
    return found


def _run(args: list[str]) -> None:
    completed = subprocess.run(args, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise SystemExit(detail[-2000:])


def _require_policy() -> None:
    path = Path("/usr/sbin/policy-rc.d")
    if not path.is_file() or "101" not in path.read_text(encoding="utf-8"):
        raise SystemExit("policy-rc.d missing")


if __name__ == "__main__":
    main()
