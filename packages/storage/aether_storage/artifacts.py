"""Content-addressed artifacts. Callers never choose the storage path."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tarfile
import threading
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aether_storage.db import Database

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


async def read_bounded_upload(upload, limit: int, chunk_size: int = 65536) -> bytes:
    """Stop reading once the upload exceeds the limit. The caller does not receive a truncated object."""
    total = 0
    parts: list[bytes] = []
    while True:
        block = await upload.read(chunk_size)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise ArtifactError("size_rejected", "产物长度超出限制")
        parts.append(block)
    if total == 0:
        raise ArtifactError("size_rejected", "产物长度超出限制或为空")
    return b"".join(parts)


MAX_SNAPSHOT_FILES = 1000
MAX_SNAPSHOT_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
ALLOWED_MEDIA_TYPES = {
    "text/plain",
    "text/markdown",
    "text/vtt",
    "application/json",
    "application/pdf",
    "application/zip",
    "application/x-tar",
}
_TERMINAL_STATES = {"resolved", "rolled_back", "reconciled"}


class ArtifactError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def assert_storage_root(root: Path) -> Path:
    resolved = root.resolve()
    home = Path.home().resolve()
    if resolved == home or home in resolved.parents:
        raise ArtifactError("host_home_rejected", "产物目录不能放在宿主 home 下")
    if not resolved.is_absolute():
        raise ArtifactError("invalid_root", "产物目录必须是绝对路径")
    return resolved


class ArtifactStore:
    def __init__(self, root: Path, database: Database) -> None:
        self.root = assert_storage_root(root)
        self.database = database
        self.root.mkdir(parents=True, exist_ok=True)
        self._journal_guard = threading.Lock()

    def put(
        self,
        *,
        owner_id: str,
        project_id: str,
        data: bytes,
        media_type: str,
        classification: str = "project_private",
        conn=None,
    ) -> dict[str, object]:
        self._validate_bytes(data, media_type)
        digest = hashlib.sha256(data).hexdigest()
        storage_key = f"{project_id}/{digest[:2]}/{digest}"
        final = self._path_for_key(storage_key)
        self._write_durable(final, data, digest)
        attempt_id = str(uuid.uuid4())
        self._journal(
            project_id,
            {
                "state": "prepared",
                "disposition": "awaiting_commit",
                "attempt_id": attempt_id,
                "artifact_id": attempt_id,
                "storage_key": storage_key,
                "sha256": digest,
                "byte_size": len(data),
                "media_type": media_type,
                "classification": classification,
            },
        )
        if conn is None:
            with self.database.transaction(owner_id=owner_id, project_id=project_id) as owned:
                chosen = self._watch_commit(owned, owner_id=owner_id, project_id=project_id, attempt_id=attempt_id, storage_key=storage_key, digest=digest)
                artifact_id, stored_media, stored_class = self._insert_on(
                    owned, project_id, attempt_id, digest, len(data), media_type, storage_key, classification
                )
                chosen["artifact_id"] = artifact_id
                chosen["media_type"] = stored_media
                chosen["classification"] = stored_class
            return self._ref(chosen["artifact_id"], project_id, digest, len(data), chosen["media_type"], chosen["classification"])
        chosen = self._watch_commit(conn, owner_id=owner_id, project_id=project_id, attempt_id=attempt_id, storage_key=storage_key, digest=digest)
        artifact_id, stored_media, stored_class = self._insert_on(
            conn, project_id, attempt_id, digest, len(data), media_type, storage_key, classification
        )
        chosen["artifact_id"] = artifact_id
        return self._ref(artifact_id, project_id, digest, len(data), stored_media, stored_class)

    def stage_without_commit(self, *, project_id: str, data: bytes, media_type: str) -> dict[str, object]:
        """Stop after a durable write. Recovery inserts the missing ledger row."""
        self._validate_bytes(data, media_type)
        digest = hashlib.sha256(data).hexdigest()
        storage_key = f"{project_id}/{digest[:2]}/{digest}"
        final = self._path_for_key(storage_key)
        self._write_durable(final, data, digest)
        artifact_id = str(uuid.uuid4())
        record = {
            "state": "prepared",
            "disposition": "staged",
            "attempt_id": artifact_id,
            "artifact_id": artifact_id,
            "storage_key": storage_key,
            "sha256": digest,
            "byte_size": len(data),
            "media_type": media_type,
            "classification": "project_private",
        }
        self._journal(project_id, record)
        return record

    def recover(self, *, owner_id: str, project_id: str) -> list[str]:
        rows = self._journal_rows(project_id)
        terminal = self._terminal_attempts(owner_id, project_id, rows)
        restored: list[str] = []
        for item in rows:
            if item.get("state") != "prepared":
                continue
            attempt = str(item.get("attempt_id") or item.get("artifact_id") or "")
            if not attempt or attempt in terminal:
                continue
            self._recover_prepared(owner_id, project_id, item, rows, restored)
            terminal.add(attempt)
        return restored

    def get(self, *, owner_id: str, project_id: str, artifact_id: str) -> tuple[bytes, str]:
        row = self._row(owner_id, project_id, artifact_id)
        if row is None or row["deleted_at"] is not None:
            raise ArtifactError("not_found", "产物不存在")
        path = self._path_for_key(row["storage_key"])
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            data = os.read(fd, row["byte_size"] + 1)
        finally:
            os.close(fd)
        if len(data) != row["byte_size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ArtifactError("hash_mismatch", "磁盘内容与账本哈希不一致")
        return data, row["media_type"]

    def tombstone(self, *, owner_id: str, project_id: str, artifact_id: str) -> None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            updated = conn.execute(
                text("UPDATE artifacts SET deleted_at = now() WHERE id = :id AND project_id = :project_id AND deleted_at IS NULL"),
                {"id": artifact_id, "project_id": project_id},
            ).rowcount
        if updated != 1:
            raise ArtifactError("not_found", "产物不存在")

    def import_archive(
        self,
        *,
        owner_id: str,
        project_id: str,
        data: bytes,
        media_type: str,
        max_files: int = MAX_SNAPSHOT_FILES,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
        max_ratio: int = MAX_COMPRESSION_RATIO,
    ) -> dict[str, object]:
        members = self._inspect_archive(data, media_type, max_files, max_bytes, max_ratio)
        payloads = self._archive_payloads(data, media_type, members)
        stored = self.put(owner_id=owner_id, project_id=project_id, data=data, media_type=media_type)
        artifact_id = str(stored["artifact_id"])
        digest = str(stored["sha256"])
        lock_path = self.root / "locks" / project_id / f"{artifact_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._publish_snapshot(project_id, artifact_id, payloads, digest, media_type)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return stored

    def _watch_commit(self, conn, *, owner_id: str, project_id: str, attempt_id: str, storage_key: str, digest: str) -> dict[str, str]:
        chosen = {"artifact_id": attempt_id, "media_type": "", "classification": ""}

        def rolled_back() -> None:
            self._journal(
                project_id,
                {
                    "state": "rolled_back",
                    "attempt_id": attempt_id,
                    "artifact_id": attempt_id,
                    "storage_key": storage_key,
                    "sha256": digest,
                },
            )

        def unknown() -> None:
            self._journal(
                project_id,
                {
                    "state": "unknown",
                    "attempt_id": attempt_id,
                    "artifact_id": chosen["artifact_id"],
                    "storage_key": storage_key,
                    "sha256": digest,
                    "reason": "commit_outcome_unknown",
                },
            )

        def committed() -> None:
            self._publish_after_commit(
                owner_id=owner_id,
                project_id=project_id,
                attempt_id=attempt_id,
                storage_key=storage_key,
                digest=digest,
            )

        conn.info.setdefault("aether_after_rollback", []).append(rolled_back)
        conn.info.setdefault("aether_after_unknown", []).append(unknown)
        conn.info.setdefault("aether_after_commit", []).append(committed)
        return chosen

    def _publish_after_commit(self, *, owner_id: str, project_id: str, attempt_id: str, storage_key: str, digest: str) -> None:
        row = self._storage_row(owner_id, project_id, storage_key)
        if row is None:
            self._journal(
                project_id,
                {
                    "state": "unknown",
                    "attempt_id": attempt_id,
                    "artifact_id": attempt_id,
                    "storage_key": storage_key,
                    "sha256": digest,
                    "reason": "row_absent_after_commit",
                },
            )
            return
        if row["deleted_at"] is not None:
            self._journal(
                project_id,
                {
                    "state": "reconciled",
                    "attempt_id": attempt_id,
                    "artifact_id": str(row["id"]),
                    "storage_key": storage_key,
                    "sha256": digest,
                    "reason": "tombstone",
                },
            )
            return
        self._mark_committed(project_id, attempt_id, str(row["id"]), storage_key, digest)

    def _mark_committed(self, project_id: str, attempt_id: str, artifact_id: str, storage_key: str, digest: str) -> None:
        self._journal(
            project_id,
            {
                "state": "resolved",
                "attempt_id": attempt_id,
                "artifact_id": artifact_id,
                "storage_key": storage_key,
                "sha256": digest,
            },
        )
        self._journal(project_id, {"state": "committed", "attempt_id": attempt_id, "artifact_id": artifact_id, "sha256": digest})

    def _terminal_attempts(self, owner_id: str, project_id: str, rows: list[dict[str, object]]) -> set[str]:
        terminal: set[str] = set()
        for item in rows:
            state = item.get("state")
            attempt = str(item.get("attempt_id") or "")
            if state in _TERMINAL_STATES and attempt:
                terminal.add(attempt)
                continue
            if state != "committed":
                continue
            artifact_id = str(item.get("artifact_id") or "")
            if not artifact_id or not self._artifact_exists(owner_id, project_id, artifact_id):
                continue
            if attempt:
                terminal.add(attempt)
            else:
                terminal.add(artifact_id)
        return terminal

    def _recover_prepared(
        self,
        owner_id: str,
        project_id: str,
        item: dict[str, object],
        rows: list[dict[str, object]],
        restored: list[str],
    ) -> None:
        attempt = str(item.get("attempt_id") or item["artifact_id"])
        disposition = str(item.get("disposition") or "staged")
        digest = str(item["sha256"])
        storage_key = str(item["storage_key"])
        stored = self._storage_row(owner_id, project_id, storage_key)
        covering = self._covering_commits(item, rows)
        covering_exists = any(self._artifact_exists(owner_id, project_id, str(entry.get("artifact_id") or "")) for entry in covering)
        if disposition == "staged" and covering and not covering_exists:
            self._journal(
                project_id,
                {
                    "state": "reconciled",
                    "attempt_id": attempt,
                    "artifact_id": attempt,
                    "storage_key": storage_key,
                    "sha256": digest,
                    "reason": "stale_commit",
                },
            )
            return
        if disposition == "staged" and not covering:
            self._require_staged_bytes(item)
            if stored is not None and stored["deleted_at"] is not None:
                self._reconcile(project_id, attempt, str(stored["id"]), storage_key, digest, "tombstone")
                return
            if stored is not None:
                self._mark_committed(project_id, attempt, str(stored["id"]), storage_key, digest)
                return
            canonical = self._insert(
                owner_id,
                project_id,
                attempt,
                digest,
                int(item["byte_size"]),
                str(item["media_type"]),
                storage_key,
                str(item["classification"]),
            )
            self._mark_committed(project_id, attempt, canonical, storage_key, digest)
            restored.append(canonical)
            return
        if stored is not None and stored["deleted_at"] is not None:
            self._reconcile(project_id, attempt, str(stored["id"]), storage_key, digest, "tombstone")
            return
        if stored is not None:
            self._mark_committed(project_id, attempt, str(stored["id"]), storage_key, digest)
            return
        if covering_exists:
            canonical = next(str(entry["artifact_id"]) for entry in covering if self._artifact_exists(owner_id, project_id, str(entry.get("artifact_id") or "")))
            self._mark_committed(project_id, attempt, canonical, storage_key, digest)
            return
        reason = "stale_commit" if covering else "commit_absent"
        self._reconcile(project_id, attempt, attempt, storage_key, digest, reason)

    def _require_staged_bytes(self, item: dict[str, object]) -> None:
        path = self._path_for_key(str(item["storage_key"]))
        if not path.is_file():
            raise ArtifactError("recover_mismatch", "恢复时期望的哈希与磁盘不一致")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"] or len(data) != int(item["byte_size"]):
            raise ArtifactError("recover_mismatch", "恢复时期望的哈希与磁盘不一致")

    def _covering_commits(self, item: dict[str, object], rows: list[dict[str, object]]) -> list[dict[str, object]]:
        attempt = str(item.get("attempt_id") or item.get("artifact_id") or "")
        digest = item.get("sha256")
        covering: list[dict[str, object]] = []
        for entry in rows:
            if entry.get("state") != "committed":
                continue
            same_attempt = entry.get("attempt_id") == attempt or entry.get("artifact_id") == attempt
            same_bytes = entry.get("sha256") == digest and entry.get("artifact_id") != attempt
            if same_attempt or same_bytes:
                covering.append(entry)
        return covering

    def _reconcile(self, project_id: str, attempt_id: str, artifact_id: str, storage_key: str, digest: str, reason: str) -> None:
        self._journal(
            project_id,
            {
                "state": "reconciled",
                "attempt_id": attempt_id,
                "artifact_id": artifact_id,
                "storage_key": storage_key,
                "sha256": digest,
                "reason": reason,
            },
        )

    def _publish_snapshot(self, project_id: str, artifact_id: str, payloads: dict[str, bytes], digest: str, media_type: str) -> None:
        destination = self.root / "snapshots" / project_id / artifact_id
        manifest = self._manifest(artifact_id, digest, media_type, payloads)
        if destination.exists() or destination.is_symlink():
            if self._tree_matches(destination, payloads):
                self._write_meta(project_id, artifact_id, manifest)
                return
            self._quarantine(destination)
        parent = self.root / "tmp" / "extract"
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / uuid.uuid4().hex
        staging.mkdir(exist_ok=False)
        try:
            self._write_members(staging, payloads)
            if not self._tree_matches(staging, payloads):
                raise ArtifactError("snapshot_mismatch", "解压结果与归档成员不一致")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._write_meta(project_id, artifact_id, manifest)

    def _manifest(self, artifact_id: str, digest: str, media_type: str, payloads: dict[str, bytes]) -> dict[str, object]:
        members = [
            {"name": name, "sha256": hashlib.sha256(blob).hexdigest(), "byte_size": len(blob)}
            for name, blob in sorted(payloads.items())
        ]
        return {
            "state": "complete",
            "artifact_id": artifact_id,
            "sha256": digest,
            "media_type": media_type,
            "members": members,
        }

    def _write_meta(self, project_id: str, artifact_id: str, manifest: dict[str, object]) -> None:
        directory = self.root / "snapshot-meta" / project_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{artifact_id}.json"
        temporary = directory / f".{artifact_id}.{uuid.uuid4().hex}.tmp"
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _tree_matches(self, destination: Path, payloads: dict[str, bytes]) -> bool:
        if destination.is_symlink() or not destination.is_dir():
            return False
        found: dict[str, bytes] = {}
        root = destination.resolve()
        for path in destination.rglob("*"):
            if path.is_symlink():
                return False
            if not path.is_file():
                continue
            resolved = path.resolve()
            if root not in resolved.parents:
                return False
            found[path.relative_to(destination).as_posix()] = path.read_bytes()
        return found == payloads

    def _quarantine(self, destination: Path) -> None:
        if not destination.exists() and not destination.is_symlink():
            return
        quarantine = self.root / "quarantine" / destination.parent.name
        quarantine.mkdir(parents=True, exist_ok=True)
        os.replace(destination, quarantine / f"{destination.name}-{uuid.uuid4().hex}")

    def _write_members(self, destination: Path, payloads: dict[str, bytes]) -> None:
        for name, blob in payloads.items():
            target = self._destination_member(destination, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o644)
            try:
                os.write(fd, blob)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _validate_bytes(self, data: bytes, media_type: str) -> None:
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise ArtifactError("media_type_rejected", "媒体类型未允许")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise ArtifactError("size_rejected", "产物长度超出限制或为空")
        if media_type == "application/json":
            try:
                json.loads(data.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ArtifactError("media_mismatch", "内容不是 JSON") from exc
        elif media_type.startswith("text/"):
            try:
                data.decode("utf-8")
            except UnicodeError as exc:
                raise ArtifactError("media_mismatch", "内容不是 UTF-8 文本") from exc
        elif media_type == "application/pdf" and not data.startswith(b"%PDF-"):
            raise ArtifactError("media_mismatch", "内容不是 PDF")
        elif media_type == "application/zip" and not data.startswith(b"PK"):
            raise ArtifactError("media_mismatch", "内容不是 ZIP")
        elif media_type == "application/x-tar":
            try:
                tarfile.open(fileobj=BytesIO(data), mode="r:")
            except tarfile.TarError as exc:
                raise ArtifactError("media_mismatch", "内容不是 tar") from exc

    def _write_durable(self, final: Path, data: bytes, digest: str) -> None:
        temporary_dir = self.root / "tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / f"{uuid.uuid4().hex}.part"
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        written = temporary.read_bytes()
        if hashlib.sha256(written).hexdigest() != digest or len(written) != len(data):
            temporary.unlink(missing_ok=True)
            raise ArtifactError("hash_mismatch", "写入后的哈希与内容不一致")
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, final)
        directory = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _path_for_key(self, storage_key: str) -> Path:
        if storage_key.startswith("/") or "\\" in storage_key or ".." in Path(storage_key).parts:
            raise ArtifactError("path_rejected", "存储路径只能由服务端生成")
        candidate = (self.root / storage_key).resolve()
        if self.root not in candidate.parents:
            raise ArtifactError("path_rejected", "存储路径越出产物根目录")
        return candidate

    @contextmanager
    def _exclusive_journal(self, project_id: str) -> Iterator[int]:
        """Lock a stable sidecar so append and tail repair cannot pass each other.

        The lock file stays on one inode. The journal itself is truncated to the
        valid prefix length and is not replaced, so a crash cannot drop that prefix.
        """
        directory = self.root / "journal"
        directory.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(directory / f"{project_id}.lock", os.O_CREAT | os.O_RDWR, 0o644)
        with self._journal_guard:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                fd = os.open(directory / f"{project_id}.jsonl", os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    yield fd
                finally:
                    os.close(fd)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def _journal(self, project_id: str, record: dict[str, object]) -> None:
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
        with self._exclusive_journal(project_id) as fd:
            self._repair_torn_tail(project_id, fd)
            os.lseek(fd, 0, os.SEEK_END)
            self._write_all(fd, line)

    def _journal_rows(self, project_id: str) -> list[dict[str, object]]:
        path = self.root / "journal" / f"{project_id}.jsonl"
        if not path.exists():
            return []
        with self._exclusive_journal(project_id) as fd:
            raw = self._repair_torn_tail(project_id, fd)
            return self._parse_journal(raw)

    def _repair_torn_tail(self, project_id: str, fd: int) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = self._read_all(fd)
        if not raw or raw.endswith(b"\n"):
            return raw
        head, _separator, tail = raw.rpartition(b"\n")
        kept = head + b"\n" if head else b""
        self._quarantine_journal_tail(project_id, tail)
        if kept:
            self._checkpoint_prefix(project_id, kept)
        os.ftruncate(fd, len(kept))
        os.fsync(fd)
        return kept

    def _checkpoint_prefix(self, project_id: str, kept: bytes) -> None:
        """Copy the valid prefix before truncating. A failed copy leaves the journal untouched."""
        directory = self.root / "journal-repair"
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f"{uuid.uuid4().hex}.prefix"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            self._write_all(fd, kept)
        except Exception:
            os.close(fd)
            temporary.unlink(missing_ok=True)
            raise
        os.close(fd)
        os.replace(temporary, directory / f"{project_id}.prefix")

    def _quarantine_journal_tail(self, project_id: str, tail: bytes) -> None:
        directory = self.root / "journal-quarantine"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{project_id}-{uuid.uuid4().hex}.tail"
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            self._write_all(fd, tail)
        finally:
            os.close(fd)

    @staticmethod
    def _parse_journal(raw: bytes) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if not raw:
            return rows
        for index, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ArtifactError("journal_corrupt", f"日志第 {index} 行损坏") from exc
        return rows

    @staticmethod
    def _read_all(fd: int) -> bytes:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ArtifactError("journal_short_write", "日志写入没有前进")
            view = view[written:]
        os.fsync(fd)

    def _insert(
        self,
        owner_id: str,
        project_id: str,
        artifact_id: str,
        digest: str,
        size: int,
        media_type: str,
        storage_key: str,
        classification: str,
        conn=None,
    ) -> str:
        if conn is None:
            with self.database.transaction(owner_id=owner_id, project_id=project_id) as owned:
                stored_id, _media, _classification = self._insert_on(
                    owned, project_id, artifact_id, digest, size, media_type, storage_key, classification
                )
                return stored_id
        stored_id, _media, _classification = self._insert_on(
            conn, project_id, artifact_id, digest, size, media_type, storage_key, classification
        )
        return stored_id

    def _insert_on(
        self,
        conn,
        project_id: str,
        artifact_id: str,
        digest: str,
        size: int,
        media_type: str,
        storage_key: str,
        classification: str,
    ) -> tuple[str, str, str]:
        existing = self._live_identity(conn, project_id, storage_key, classification, media_type)
        if existing is not None:
            return existing
        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO artifacts (id, project_id, sha256, byte_size, media_type, storage_key, classification) "
                        "VALUES (:id, :project_id, :sha256, :byte_size, :media_type, :storage_key, :classification)"
                    ),
                    {
                        "id": artifact_id,
                        "project_id": project_id,
                        "sha256": digest,
                        "byte_size": size,
                        "media_type": media_type,
                        "storage_key": storage_key,
                        "classification": classification,
                    },
                )
        except IntegrityError:
            raced = self._live_identity(conn, project_id, storage_key, classification, media_type)
            if raced is not None:
                return raced
            raise
        return artifact_id, media_type, classification

    def _live_identity(self, conn, project_id: str, storage_key: str, classification: str, media_type: str) -> tuple[str, str, str] | None:
        row = conn.execute(
            text(
                "SELECT id::text AS id, media_type, classification FROM artifacts "
                "WHERE project_id = :project_id AND storage_key = :storage_key AND deleted_at IS NULL "
                "AND classification = :classification AND media_type = :media_type"
            ),
            {"project_id": project_id, "storage_key": storage_key, "classification": classification, "media_type": media_type},
        ).mappings().first()
        if row is None:
            return None
        return str(row["id"]), str(row["media_type"]), str(row["classification"])

    def _row(self, owner_id: str, project_id: str, artifact_id: str) -> dict | None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            row = conn.execute(
                text("SELECT id::text, sha256, byte_size, media_type, storage_key, deleted_at FROM artifacts WHERE id = :id AND project_id = :project_id"),
                {"id": artifact_id, "project_id": project_id},
            ).mappings().first()
        return None if row is None else dict(row)

    def _artifact_exists(self, owner_id: str, project_id: str, artifact_id: str) -> bool:
        return self._row(owner_id, project_id, artifact_id) is not None

    def _storage_row(self, owner_id: str, project_id: str, storage_key: str) -> dict | None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            row = conn.execute(
                text(
                    "SELECT id::text AS id, sha256, byte_size, media_type, storage_key, classification, deleted_at "
                    "FROM artifacts WHERE project_id = :project_id AND storage_key = :storage_key "
                    "ORDER BY (deleted_at IS NULL) DESC, created_at DESC LIMIT 1"
                ),
                {"project_id": project_id, "storage_key": storage_key},
            ).mappings().first()
        return None if row is None else dict(row)

    def _inspect_archive(self, data: bytes, media_type: str, max_files: int, max_bytes: int, max_ratio: int) -> list[str]:
        names: list[str] = []
        total = 0
        if media_type == "application/zip":
            with zipfile.ZipFile(BytesIO(data)) as archive:
                for info in archive.infolist():
                    self._reject_member_name(info.filename)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        raise ArtifactError("symlink_rejected", "压缩包包含符号链接")
                    if info.file_size and info.compress_size and info.file_size / info.compress_size > max_ratio:
                        raise ArtifactError("ratio_rejected", "压缩比超过上限")
                    total += info.file_size
                    if not info.is_dir():
                        names.append(info.filename)
        elif media_type == "application/x-tar":
            with tarfile.open(fileobj=BytesIO(data), mode="r:") as archive:
                for info in archive.getmembers():
                    self._reject_member_name(info.name)
                    if info.issym() or info.islnk():
                        raise ArtifactError("link_rejected", "压缩包包含符号链接或硬链接")
                    if not info.isdir():
                        total += info.size
                        names.append(info.name)
        else:
            raise ArtifactError("media_type_rejected", "快照只接受 zip 或 tar")
        if len(names) > max_files or total > max_bytes:
            raise ArtifactError("archive_limit", "解压后的文件数量或大小超过上限")
        seen: set[str] = set()
        for name in names:
            canonical = self._canonical_member_name(name)
            if canonical in seen:
                raise ArtifactError("archive_collision", "归档成员规范化后出现重名")
            seen.add(canonical)
        return names

    def _archive_payloads(self, data: bytes, media_type: str, names: list[str]) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        if media_type == "application/zip":
            with zipfile.ZipFile(BytesIO(data)) as archive:
                for name in names:
                    payloads[self._canonical_member_name(name)] = archive.read(name)
            return payloads
        with tarfile.open(fileobj=BytesIO(data), mode="r:") as archive:
            for name in names:
                extracted = archive.extractfile(name)
                if extracted is None:
                    raise ArtifactError("extract_failed", "无法读取归档成员")
                payloads[self._canonical_member_name(name)] = extracted.read()
        return payloads

    def _destination_member(self, destination: Path, name: str) -> Path:
        name = self._canonical_member_name(name)
        target = (destination / name).resolve()
        if destination.resolve() not in target.parents and target != destination.resolve():
            raise ArtifactError("path_rejected", "解压路径越界")
        return target

    @staticmethod
    def _reject_member_name(name: str) -> None:
        path = PurePosixPath(name)
        if not name or name.startswith("/") or "\\" in name or ".." in path.parts or path.is_absolute():
            raise ArtifactError("path_rejected", "归档成员路径不合法")

    @staticmethod
    def _canonical_member_name(name: str) -> str:
        ArtifactStore._reject_member_name(name)
        parts = [part for part in PurePosixPath(name).parts if part not in {"", "."}]
        canonical = "/".join(parts)
        if not canonical or canonical == "." or ".." in parts:
            raise ArtifactError("path_rejected", "归档文件不能指向快照根目录")
        return canonical

    @staticmethod
    def _ref(artifact_id: str, project_id: str, digest: str, size: int, media_type: str, classification: str) -> dict[str, object]:
        return {
            "artifact_id": artifact_id,
            "project_id": project_id,
            "sha256": digest,
            "byte_size": size,
            "media_type": media_type,
            "classification": classification,
        }
