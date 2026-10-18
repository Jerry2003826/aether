"""The OpenHands sandbox image's identity is a hash of its Dockerfile and build context (sandbox_tag),
so building that Dockerfile must always install the same packages. Debian packages come only from
snapshot.debian.org at one fixed timestamp, set up before apt-get update in the same RUN, and every
package named on apt-get install carries an exact version (Codex openhands-sandbox.Dockerfile:11)."""

from __future__ import annotations

import re

from aether_worker.openhands_image import DOCKERFILE, _instructions, repository_root

_SNAPSHOT = re.compile(r"https://snapshot\.debian\.org/archive/(debian|debian-security)/(\d{8}T\d{6}Z)\b")


def _apt_runs() -> list[str]:
    text = (repository_root() / DOCKERFILE).read_text(encoding="utf-8")
    return [line for line in _instructions(text) if line.startswith("RUN ") and "apt-get" in line]


def test_the_sandbox_installs_debian_packages() -> None:
    assert _apt_runs(), "the sandbox installs git with apt-get"


def test_apt_reads_only_one_debian_snapshot_set_up_before_update() -> None:
    for run in _apt_runs():
        update = run.index("apt-get")
        before = run[:update]
        found = _SNAPSHOT.findall(before)
        assert {archive for archive, _stamp in found} == {"debian", "debian-security"}, run
        assert len({stamp for _archive, stamp in found}) == 1, run
        assert "deb.debian.org" not in run, run
        # The base image's own sources are removed, so apt sees the snapshot only.
        removed = {path for command in re.findall(r"rm -f ([^&]+)", before) for path in command.split()}
        assert {"/etc/apt/sources.list.d/debian.sources", "/etc/apt/sources.list"} <= removed, run


def test_every_apt_package_carries_an_exact_version() -> None:
    for run in _apt_runs():
        for install in re.findall(r"apt-get install ([^&]+)", run):
            packages = [part for part in install.split() if not part.startswith("-")]
            assert packages, run
            for package in packages:
                name, separator, version = package.partition("=")
                assert separator and name and re.fullmatch(r"[0-9]+:[0-9A-Za-z.+~-]+|[0-9][0-9A-Za-z.+~-]*", version), package
