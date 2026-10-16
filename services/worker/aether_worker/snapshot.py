"""Race-safe snapshots of an authorised directory.

Opens use ``O_NOFOLLOW`` and directory file descriptors. A symlink, mount, or
non-regular file is rejected instead of resolved and opened later.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Skip = Callable[[tuple[str, ...], bool], bool]


_REJECTED = "snapshot_rejected"


class SnapshotRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject() -> SnapshotRejected:
    return SnapshotRejected(_REJECTED)


@dataclass(frozen=True)
class _Node:
    parts: tuple[str, ...]
    data: bytes | None
    mode: int


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path: str | Path, dir_fd: int | None = None) -> int:
    try:
        if dir_fd is None:
            return os.open(path, _DIR_FLAGS)
        return os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _reject() from exc


def _list_names(dir_fd: int) -> list[str]:
    scan_fd = os.dup(dir_fd)
    try:
        iterator = os.scandir(scan_fd)
    except OSError as exc:
        os.close(scan_fd)
        raise _reject() from exc
    with iterator:
        return sorted(entry.name for entry in iterator)


def _bad_name(name: str) -> bool:
    return name in {"", ".", ".."} or "/" in name or "\x00" in name


def _read_file(dir_fd: int, name: str, root_dev: int) -> bytes:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _reject() from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != root_dev:
            raise _reject()
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _walk(dir_fd: int, root_dev: int, prefix: tuple[str, ...], skip: Skip, found: list[_Node]) -> None:
    for name in _list_names(dir_fd):
        if _bad_name(name):
            raise _reject()
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        is_link = stat.S_ISLNK(info.st_mode)
        is_dir = stat.S_ISDIR(info.st_mode) and not is_link
        parts = prefix + (name,)
        if skip(parts, is_dir):
            continue
        if is_link:
            raise SnapshotRejected("snapshot_symlink")
        if info.st_dev != root_dev:
            raise _reject()
        if is_dir:
            _descend(dir_fd, name, root_dev, parts, skip, found)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise _reject()
        found.append(_Node(parts, _read_file(dir_fd, name, root_dev), stat.S_IMODE(info.st_mode) & 0o777))


def _descend(dir_fd: int, name: str, root_dev: int, parts: tuple[str, ...], skip: Skip, found: list[_Node]) -> None:
    child = _open_directory(name, dir_fd)
    try:
        child_stat = os.fstat(child)
        if not stat.S_ISDIR(child_stat.st_mode) or child_stat.st_dev != root_dev:
            raise _reject()
        found.append(_Node(parts, None, 0o755))
        _walk(child, root_dev, parts, skip, found)
    finally:
        os.close(child)


def scan(root: Path, skip: Skip) -> list[_Node]:
    root_fd = _open_directory(root)
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise _reject()
        found: list[_Node] = []
        _walk(root_fd, root_stat.st_dev, (), skip, found)
        return found
    finally:
        os.close(root_fd)


def read_tree(root: Path, *, skip: Skip) -> list[tuple[str, bytes]]:
    files = [("/".join(entry.parts), entry.data) for entry in scan(root, skip) if entry.data is not None]
    files.sort(key=lambda item: Path(item[0]))
    return files


def read_regular(root: Path, relative: tuple[str, ...]) -> bytes:
    if not relative or any(_bad_name(part) for part in relative):
        raise _reject()
    root_fd = _open_directory(root)
    owned: list[int] = []
    try:
        root_dev = os.fstat(root_fd).st_dev
        dir_fd = root_fd
        for name in relative[:-1]:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_dev != root_dev:
                raise _reject()
            child = _open_directory(name, dir_fd)
            owned.append(child)
            dir_fd = child
        return _read_file(dir_fd, relative[-1], root_dev)
    finally:
        for fd in reversed(owned):
            os.close(fd)
        os.close(root_fd)


def _clear_directory(dir_fd: int) -> None:
    for name in _list_names(dir_fd):
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=dir_fd)
            continue
        child = _open_directory(name, dir_fd)
        try:
            _clear_directory(child)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=dir_fd)


def ensure_real_directory(destination: Path, *, replace: bool) -> None:
    name = destination.name
    if _bad_name(name):
        raise _reject()
    parent_fd = _open_directory(destination.parent)
    try:
        _ensure_named_dir(parent_fd, name, replace=replace)
    finally:
        os.close(parent_fd)


def _ensure_named_dir(parent_fd: int, name: str, *, replace: bool) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        _mkdir(parent_fd, name)
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        if not replace:
            raise _reject()
        os.unlink(name, dir_fd=parent_fd)
        _mkdir(parent_fd, name)
        return
    if not replace:
        return
    child = _open_directory(name, parent_fd)
    try:
        _clear_directory(child)
        os.fchmod(child, 0o755)
    finally:
        os.close(child)


def _mkdir(parent_fd: int, name: str) -> None:
    os.mkdir(name, 0o755, dir_fd=parent_fd)
    child = _open_directory(name, parent_fd)
    try:
        os.fchmod(child, 0o755)
    finally:
        os.close(child)


def _open_created_dir(dir_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o755, dir_fd=dir_fd)
    except FileExistsError:
        pass
    child = _open_directory(name, dir_fd)
    os.fchmod(child, 0o755)
    return child


def _write_file(root_fd: int, parts: tuple[str, ...], data: bytes, mode: int) -> None:
    fd = os.dup(root_fd)
    try:
        for name in parts[:-1]:
            child = _open_created_dir(fd, name)
            os.close(fd)
            fd = child
        try:
            out = os.open(parts[-1], _WRITE_FLAGS, 0o644, dir_fd=fd)
        except OSError as exc:
            raise _reject() from exc
        try:
            view = memoryview(data)
            while view:
                written = os.write(out, view)
                if written <= 0:
                    raise _reject()
                view = view[written:]
            os.fchmod(out, mode & 0o777)
        finally:
            os.close(out)
    finally:
        os.close(fd)


def write_entries(destination: Path, entries: list[_Node], *, file_mode: int | None) -> None:
    ensure_real_directory(destination, replace=True)
    root_fd = _open_directory(destination)
    try:
        for entry in entries:
            if entry.data is None:
                fd = os.dup(root_fd)
                try:
                    for name in entry.parts:
                        child = _open_created_dir(fd, name)
                        os.close(fd)
                        fd = child
                finally:
                    os.close(fd)
                continue
            mode = entry.mode if file_mode is None else file_mode
            _write_file(root_fd, entry.parts, entry.data, mode)
    finally:
        os.close(root_fd)


def copy_tree(source: Path, destination: Path, *, skip: Skip, file_mode: int | None) -> None:
    write_entries(destination, scan(source, skip), file_mode=file_mode)
