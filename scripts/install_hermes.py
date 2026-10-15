"""Install the pinned Hermes checkout and run its real version probe.

The GitHub archive checksum and the tag commit are both required. The process
that prints the version is the git checkout, so the CLI can read the tag.
"""

from __future__ import annotations

import argparse
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


def _ensure_checkout(dest: Path, lock: dict[str, object]) -> None:
    repository = str(lock["repository"])
    tag = str(lock["tag"])
    commit = str(lock["commit"])
    if (dest / ".git").is_dir() and _git(dest, "rev-parse", "HEAD") == commit:
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", "--branch", tag, repository, str(dest)], check=True)


def install(dest: Path) -> str:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    archive = dest.parent / f"hermes-v{lock['version']}.tar.gz"
    if not archive.is_file() or _sha256(archive) != lock["archive_sha256"]:
        _download(str(lock["archive_url"]), archive)
    actual = _sha256(archive)
    if actual != lock["archive_sha256"]:
        archive.unlink(missing_ok=True)
        _fail(f"hermes archive sha256 {actual} != {lock['archive_sha256']}")
    _ensure_checkout(dest, lock)
    if _git(dest, "rev-parse", "HEAD") != lock["commit"]:
        _fail(f"hermes HEAD is not {lock['commit']}")
    if _git(dest, "rev-parse", str(lock["tag"])) != lock["tag_object"]:
        _fail(f"hermes tag object is not {lock['tag_object']}")
    if _git(dest, "rev-parse", f"{lock['tag']}^{{}}") != lock["commit"]:
        _fail(f"hermes tag does not peel to {lock['commit']}")
    for relative in lock["bound_files"]:
        member = f"{lock['archive_root']}/{relative}"
        cloned = hashlib.sha256((dest / str(relative)).read_bytes()).hexdigest()
        if cloned != _archive_sha(archive, member):
            _fail(f"{relative} does not match the checksummed archive")
    env = _clean_env()
    subprocess.run(["uv", "python", "install", str(lock["python"])], check=True, env=env)
    subprocess.run(
        ["uv", "sync", "--python", str(lock["python"]), "--frozen", "--no-dev"],
        cwd=dest,
        check=True,
        env=env,
    )
    binary = dest / ".venv" / "bin" / "hermes"
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
    prefix = f"Hermes Agent v{lock['version']} "
    if completed.returncode != 0 or not version_line.startswith(prefix) or str(lock["commit"])[:12] not in version_line:
        _fail(f"hermes version probe failed: {completed.returncode} {version_line}")
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with open(github_path, "a", encoding="utf-8") as handle:
            handle.write(str(binary.parent) + "\n")
    return str(binary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args()
    binary = install(args.dest)
    print(f"hermes_bin={binary}")


if __name__ == "__main__":
    main()
