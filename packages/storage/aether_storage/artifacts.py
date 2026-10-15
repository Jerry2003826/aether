"""Content-addressed artifacts. Callers never choose the storage path."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from sqlalchemy import text

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

    def put(self, *, owner_id: str, project_id: str, data: bytes, media_type: str, classification: str = "project_private", conn=None) -> dict[str, object]:
        self._validate_bytes(data, media_type)
        digest = hashlib.sha256(data).hexdigest()
        storage_key = f"{project_id}/{digest[:2]}/{digest}"
        final = self._path_for_key(storage_key)
        self._write_durable(final, data, digest)
        artifact_id = str(uuid.uuid4())
        self._journal(project_id, {"state": "prepared", "artifact_id": artifact_id, "storage_key": storage_key, "sha256": digest, "byte_size": len(data), "media_type": media_type, "classification": classification})
        stored_id = self._insert(owner_id, project_id, artifact_id, digest, len(data), media_type, storage_key, classification, conn=conn)
        self._journal(project_id, {"state": "committed", "artifact_id": stored_id, "sha256": digest})
        return self._ref(stored_id, project_id, digest, len(data), media_type, classification)

    def stage_without_commit(self, *, project_id: str, data: bytes, media_type: str) -> dict[str, object]:
        """Stop after a durable write. Recovery inserts the missing ledger row."""
        self._validate_bytes(data, media_type)
        digest = hashlib.sha256(data).hexdigest()
        storage_key = f"{project_id}/{digest[:2]}/{digest}"
        final = self._path_for_key(storage_key)
        self._write_durable(final, data, digest)
        artifact_id = str(uuid.uuid4())
        record = {"state": "prepared", "artifact_id": artifact_id, "storage_key": storage_key, "sha256": digest, "byte_size": len(data), "media_type": media_type, "classification": "project_private"}
        self._journal(project_id, record)
        return record

    def recover(self, *, owner_id: str, project_id: str) -> list[str]:
        committed = {item["artifact_id"] for item in self._journal_rows(project_id) if item.get("state") == "committed"}
        restored: list[str] = []
        for item in self._journal_rows(project_id):
            if item.get("state") != "prepared" or item["artifact_id"] in committed:
                continue
            path = self._path_for_key(item["storage_key"])
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != item["sha256"] or len(data) != item["byte_size"]:
                raise ArtifactError("recover_mismatch", "恢复时期望的哈希与磁盘不一致")
            self._insert(owner_id, project_id, item["artifact_id"], digest, len(data), item["media_type"], item["storage_key"], item["classification"])
            self._journal(project_id, {"state": "committed", "artifact_id": item["artifact_id"], "sha256": digest})
            restored.append(item["artifact_id"])
            committed.add(item["artifact_id"])
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

    def import_archive(self, *, owner_id: str, project_id: str, data: bytes, media_type: str, max_files: int = MAX_SNAPSHOT_FILES, max_bytes: int = MAX_SNAPSHOT_BYTES, max_ratio: int = MAX_COMPRESSION_RATIO) -> dict[str, object]:
        members = self._inspect_archive(data, media_type, max_files, max_bytes, max_ratio)
        stored = self.put(owner_id=owner_id, project_id=project_id, data=data, media_type=media_type)
        destination = self.root / "snapshots" / project_id / str(stored["artifact_id"])
        destination.mkdir(parents=True, exist_ok=False)
        self._extract(data, media_type, destination, members)
        return stored

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

    def _journal(self, project_id: str, record: dict[str, object]) -> None:
        directory = self.root / "journal"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{project_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _journal_rows(self, project_id: str) -> list[dict[str, object]]:
        path = self.root / "journal" / f"{project_id}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _insert(self, owner_id: str, project_id: str, artifact_id: str, digest: str, size: int, media_type: str, storage_key: str, classification: str, conn=None) -> str:
        if conn is None:
            with self.database.transaction(owner_id=owner_id, project_id=project_id) as owned:
                return self._insert_on(owned, project_id, artifact_id, digest, size, media_type, storage_key, classification)
        return self._insert_on(conn, project_id, artifact_id, digest, size, media_type, storage_key, classification)

    def _insert_on(self, conn, project_id: str, artifact_id: str, digest: str, size: int, media_type: str, storage_key: str, classification: str) -> str:
        existing = conn.execute(text("SELECT id::text FROM artifacts WHERE project_id = :project_id AND storage_key = :storage_key AND deleted_at IS NULL"), {"project_id": project_id, "storage_key": storage_key}).scalar()
        if existing is not None:
            return str(existing)
        conn.execute(
            text(
                "INSERT INTO artifacts (id, project_id, sha256, byte_size, media_type, storage_key, classification) "
                "VALUES (:id, :project_id, :sha256, :byte_size, :media_type, :storage_key, :classification)"
            ),
            {"id": artifact_id, "project_id": project_id, "sha256": digest, "byte_size": size, "media_type": media_type, "storage_key": storage_key, "classification": classification},
        )
        return artifact_id

    def _row(self, owner_id: str, project_id: str, artifact_id: str) -> dict | None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            row = conn.execute(
                text("SELECT id::text, sha256, byte_size, media_type, storage_key, deleted_at FROM artifacts WHERE id = :id AND project_id = :project_id"),
                {"id": artifact_id, "project_id": project_id},
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
        return names

    def _extract(self, data: bytes, media_type: str, destination: Path, names: list[str]) -> None:
        if media_type == "application/zip":
            with zipfile.ZipFile(BytesIO(data)) as archive:
                for name in names:
                    target = self._destination_member(destination, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
        else:
            with tarfile.open(fileobj=BytesIO(data), mode="r:") as archive:
                for name in names:
                    member = archive.getmember(name)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ArtifactError("extract_failed", "无法读取归档成员")
                    target = self._destination_member(destination, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(extracted.read())

    def _destination_member(self, destination: Path, name: str) -> Path:
        self._reject_member_name(name)
        target = (destination / name).resolve()
        if destination.resolve() not in target.parents and target != destination.resolve():
            raise ArtifactError("path_rejected", "解压路径越界")
        return target

    @staticmethod
    def _reject_member_name(name: str) -> None:
        path = Path(name)
        if not name or name.startswith("/") or "\\" in name or ".." in path.parts or path.is_absolute():
            raise ArtifactError("path_rejected", "归档成员路径不合法")

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
