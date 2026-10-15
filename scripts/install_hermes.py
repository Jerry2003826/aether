"""Install the pinned Hermes checkout and run its real version probe.

The GitHub archive checksum and the tag commit are both required. The process
that prints the version is the git checkout, so the CLI can read the tag.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "config" / "hermes.lock.json"
INSTALL_DIR = ROOT / ".hermes"
REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
TAG = "v0.22.1"
COMMIT = "297552a9d646119871391d202509822cd86330df"
TAG_OBJECT = "28106ea1a3230177edb30a0a4e8a099d839bbc2f"
VERSION = "0.22.1"
ARCHIVE_URL = "https://github.com/NousResearch/hermes-agent/archive/refs/tags/v0.22.1.tar.gz"
ARCHIVE_SHA256 = "45ebe864dbf65ecd31693a811ddd6e2e449b1878b835b563cf0a28d6a9c4bf38"
ARCHIVE_ROOT = "hermes-agent-0.22.1"
PYTHON = "3.14"
OPENAI_SDK = "2.24.0"
BOUND_FILES = ("pyproject.toml", "hermes_cli/main.py")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fail(message: str) -> None:
    raise SystemExit(message)


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "aether-hermes-install"})
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    partial.replace(destination)


def _archive_sha(archive: Path, member: str) -> str:
    with tarfile.open(archive) as bundle:
        extracted = bundle.extractfile(member)
        if extracted is None:
            _fail(f"archive missing {member}")
        return hashlib.sha256(extracted.read()).hexdigest()


def _git(dest: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=dest,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        _fail(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONPATH"):
        env.pop(key, None)
    return env


def _lock_matches(lock: dict[str, object]) -> bool:
    return lock == {
        "name": "hermes-agent",
        "version": VERSION,
        "tag": TAG,
        "repository": REPOSITORY,
        "commit": COMMIT,
        "tag_object": TAG_OBJECT,
        "archive_url": ARCHIVE_URL,
        "archive_sha256": ARCHIVE_SHA256,
        "archive_root": ARCHIVE_ROOT,
        "python": PYTHON,
        "openai_sdk": OPENAI_SDK,
        "bound_files": list(BOUND_FILES),
    }


def _ensure_checkout() -> None:
    if (INSTALL_DIR / ".git").is_dir() and _git(INSTALL_DIR, "rev-parse", "HEAD") == COMMIT:
        return
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
    subprocess.run(["git", "clone", "--depth", "1", "--branch", TAG, REPOSITORY, ".hermes"], cwd=ROOT, check=True)


def install() -> str:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if not _lock_matches(lock):
        _fail("hermes.lock.json does not match the pinned install")
    archive = ROOT / f".hermes-v{VERSION}.tar.gz"
    if not archive.is_file() or _sha256(archive) != ARCHIVE_SHA256:
        _download(ARCHIVE_URL, archive)
    actual = _sha256(archive)
    if actual != ARCHIVE_SHA256:
        archive.unlink(missing_ok=True)
        _fail(f"hermes archive sha256 {actual} != {ARCHIVE_SHA256}")
    _ensure_checkout()
    if _git(INSTALL_DIR, "rev-parse", "HEAD") != COMMIT:
        _fail(f"hermes HEAD is not {COMMIT}")
    if _git(INSTALL_DIR, "rev-parse", TAG) != TAG_OBJECT:
        _fail(f"hermes tag object is not {TAG_OBJECT}")
    if _git(INSTALL_DIR, "rev-parse", f"{TAG}^{{}}") != COMMIT:
        _fail(f"hermes tag does not peel to {COMMIT}")
    for relative in BOUND_FILES:
        member = f"{ARCHIVE_ROOT}/{relative}"
        cloned = hashlib.sha256((INSTALL_DIR / relative).read_bytes()).hexdigest()
        if cloned != _archive_sha(archive, member):
            _fail(f"{relative} does not match the checksummed archive")
    env = _clean_env()
    subprocess.run(["uv", "python", "install", PYTHON], check=True, env=env)
    subprocess.run(
        ["uv", "sync", "--python", PYTHON, "--frozen", "--no-dev"],
        cwd=INSTALL_DIR,
        check=True,
        env=env,
    )
    binary = INSTALL_DIR / ".venv" / "bin" / "hermes"
    if not binary.is_file():
        _fail("hermes executable was not installed")
    home = Path(tempfile.mkdtemp(prefix="hermes-home-"))
    probe_env = env.copy()
    probe_env["HERMES_HOME"] = str(home)
    probe_env["PATH"] = str(binary.parent) + os.pathsep + probe_env.get("PATH", "")
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=probe_env,
        timeout=90,
    )
    print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    version_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    prefix = f"Hermes Agent v{VERSION} "
    if completed.returncode != 0 or not version_line.startswith(prefix) or COMMIT[:8] not in version_line:
        _fail(f"hermes version probe failed: {completed.returncode} {version_line}")
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with open(github_path, "a", encoding="utf-8") as handle:
            handle.write(str(binary.parent) + "\n")
    return str(binary)


def main() -> None:
    binary = install()
    print(f"hermes_bin={binary}")


if __name__ == "__main__":
    main()
