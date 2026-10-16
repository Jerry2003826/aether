"""Host snapshots must not follow candidate symlinks before the sandbox starts."""

import hashlib
import os
import signal
import stat
import struct
from pathlib import Path

import pytest

from aether_worker.verification import VerificationError, _stage, export_candidate, prepare_workspace

SECRET = "SYNTHETIC_FIXTURE_ONLY_NO_SECRETS"


def _project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "util.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("control\n", encoding="utf-8")
    git = root / ".git"
    git.mkdir()
    (git / "config").write_text("do-not-copy\n", encoding="utf-8")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-313.pyc").write_bytes(b"pyc")
    (root / "conftest.py").write_text("def pytest_configure(config):\n    pass\n", encoding="utf-8")
    (root / "test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "pytest.py").write_text("print('shadow')\n", encoding="utf-8")


def _digest(files: list[tuple[str, bytes]]) -> tuple[list[dict[str, str]], str]:
    ordered = sorted(files, key=lambda item: Path(item[0]))
    manifest = [{"path": path, "sha256": hashlib.sha256(data).hexdigest()} for path, data in ordered]
    digest = hashlib.sha256("\n".join(f"{item['sha256']} {item['path']}" for item in manifest).encode()).hexdigest()
    return manifest, digest


def test_regular_project_files_are_exported_and_copied(tmp_path: Path) -> None:
    source = tmp_path / "project"
    _project(source)
    exported = export_candidate(source)
    expected_files = [
        ("README.md", b"control\n"),
        ("app.py", b"VALUE = 1\n"),
        ("conftest.py", b"def pytest_configure(config):\n    pass\n"),
        ("pkg/__init__.py", b""),
        ("pkg/util.py", b"def answer():\n    return 1\n"),
        ("pytest.py", b"print('shadow')\n"),
        ("test_app.py", b"def test_x():\n    pass\n"),
    ]
    manifest, digest = _digest(expected_files)
    assert exported["files"] == manifest
    assert exported["candidate_sha256"] == digest

    workspace = tmp_path / "workspace"
    prepare_workspace(source, workspace)
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (workspace / "pkg" / "util.py").read_text(encoding="utf-8") == "def answer():\n    return 1\n"
    assert not (workspace / ".git").exists()
    assert not (workspace / "__pycache__").exists()
    assert not (workspace / "conftest.py").exists()
    assert (workspace / "test_app.py").is_file()

    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_bound.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    (suite / "conftest.py").write_text("SENTINEL = 1\n", encoding="utf-8")
    staged = tmp_path / "staged"
    _stage(source, suite, staged)
    assert (staged / "src" / "app.py").read_bytes() == b"VALUE = 1\n"
    assert (staged / "src" / "pkg" / "util.py").is_file()
    assert not (staged / "src" / "test_app.py").exists()
    assert not (staged / "src" / "conftest.py").exists()
    assert not (staged / "src" / "pytest.py").exists()
    assert (staged / "suite" / "test_bound.py").is_file()
    assert not (staged / "suite" / "conftest.py").exists()
    assert (staged / "src" / "app.py").stat().st_mode & 0o777 == 0o644
    for directory in (
        workspace,
        workspace / "pkg",
        staged,
        staged / "src",
        staged / "src" / "pkg",
        staged / "suite",
    ):
        mode = directory.stat().st_mode
        assert stat.S_ISDIR(mode)
        assert mode & 0o007 == 0
        assert _named_user_perm(directory, 65534) == 0o5


def _named_user_perm(path: Path, uid: int) -> int | None:
    raw = os.getxattr(path, "system.posix_acl_access", follow_symlinks=False)
    version = struct.unpack_from("<I", raw)[0]
    assert version == 2
    offset = 4
    found: int | None = None
    while offset + 8 <= len(raw):
        tag, perm, ident = struct.unpack_from("<HHI", raw, offset)
        if tag == 0x02 and ident == uid:
            found = perm
        offset += 8
    return found


def _raises_without_secret(action, outside: Path) -> None:
    with pytest.raises(VerificationError):
        action()
    if outside.is_file():
        assert outside.read_text(encoding="utf-8") == SECRET


@pytest.mark.parametrize("mode", ["export", "stage", "workspace"])
def test_file_symlink_outside_the_root_is_rejected(mode: str, tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "app.py").write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET, encoding="utf-8")
    (candidate / "leak.py").symlink_to(outside)
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")

    def action() -> None:
        if mode == "export":
            export_candidate(candidate)
        elif mode == "stage":
            _stage(candidate, suite, tmp_path / "staged")
        else:
            prepare_workspace(candidate, tmp_path / "workspace")

    _raises_without_secret(action, outside)
    staged = tmp_path / "staged" / "src" / "leak.py"
    copied = tmp_path / "workspace" / "leak.py"
    assert not staged.exists()
    assert not copied.exists()


@pytest.mark.parametrize("mode", ["export", "stage", "workspace"])
def test_directory_symlink_is_not_walked(mode: str, tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "app.py").write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text(SECRET, encoding="utf-8")
    (candidate / "nested").symlink_to(outside, target_is_directory=True)
    suite = tmp_path / "suite"
    suite.mkdir()

    def action() -> None:
        if mode == "export":
            export_candidate(candidate)
        elif mode == "stage":
            _stage(candidate, suite, tmp_path / "staged")
        else:
            prepare_workspace(candidate, tmp_path / "workspace")

    _raises_without_secret(action, secret)
    assert not (tmp_path / "staged" / "src" / "nested" / "secret.py").exists()
    assert not (tmp_path / "workspace" / "nested" / "secret.py").exists()


def test_internal_symlink_is_not_a_regular_file(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "app.py").write_text("ok\n", encoding="utf-8")
    (candidate / "alias.py").symlink_to("app.py")
    with pytest.raises(VerificationError):
        export_candidate(candidate)


def test_root_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "app.py").write_text("hidden\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(VerificationError):
        export_candidate(link)


def test_fifo_inside_the_root_is_not_opened(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    os.mkfifo(candidate / "pipe")

    def alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError("snapshot blocked on a fifo")

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(2)
    try:
        with pytest.raises(VerificationError):
            export_candidate(candidate)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_suite_symlink_is_rejected_by_stage(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "app.py").write_text("ok\n", encoding="utf-8")
    suite = tmp_path / "suite"
    suite.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET, encoding="utf-8")
    (suite / "test_leak.py").symlink_to(outside)
    with pytest.raises(VerificationError):
        _stage(candidate, suite, tmp_path / "staged")
    assert outside.read_text(encoding="utf-8") == SECRET
    assert not (tmp_path / "staged" / "suite" / "test_leak.py").exists()


def test_prepare_replaces_a_destination_symlink_without_writing_outside(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("hello\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_text("keep\n", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.symlink_to(outside, target_is_directory=True)
    prepare_workspace(source, destination)
    assert (outside / "marker.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (outside / "app.py").exists()
    assert destination.is_dir()
    assert not destination.is_symlink()
    assert (destination / "app.py").read_text(encoding="utf-8") == "hello\n"


def test_snapshot_uses_nofollow_directory_fds_and_does_not_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("ok\n", encoding="utf-8")
    opened: list[tuple[str | os.PathLike[str] | int, int, int | None]] = []
    real_open = os.open

    def spy(path: str | os.PathLike[str] | int, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened.append((path, flags, dir_fd))
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def refuse_resolve(self: Path, strict: bool = False) -> Path:
        raise AssertionError(f"snapshot must not resolve-then-open {self} strict={strict}")

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(Path, "resolve", refuse_resolve)
    export_candidate(source)
    assert any(flags & os.O_NOFOLLOW for _, flags, _ in opened)
    assert any(dir_fd is not None and flags & os.O_NOFOLLOW for _, flags, dir_fd in opened)
