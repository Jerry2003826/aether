import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aether_storage.artifacts import MAX_UPLOAD_BYTES, ArtifactError, ArtifactStore, assert_storage_root
from tests.integration.test_auth import ORIGIN, _bootstrap

PASSPHRASE_HEADERS = ORIGIN


def _project(client: TestClient) -> tuple[str, str, dict[str, str]]:
    _bootstrap(client)
    session = client.get("/v1/auth/session").json()
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    created = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "artifact-project"}, json={"name": "资料"})
    assert created.status_code == 201, created.text
    return session["owner_id"], created.json()["id"], headers


def test_upload_is_content_addressed_and_isolated(client: TestClient, api_db) -> None:
    owner_id, project_id, headers = _project(client)
    other = client.post("/v1/projects", headers={**headers, "Idempotency-Key": "artifact-other"}, json={"name": "另一个"})
    assert other.status_code == 201
    payload = "甲项目的不可变原文".encode()
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": "upload-key-01"},
        files={"file": ("ignored-name.txt", payload, "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["byte_size"] == len(payload)
    assert body["media_type"] == "text/plain"
    assert "ignored-name" not in body["sha256"]
    replay = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": "upload-key-01"},
        files={"file": ("ignored-name.txt", payload, "text/plain")},
    )
    assert replay.json()["artifact_id"] == body["artifact_id"]
    downloaded = client.get(f"/v1/projects/{project_id}/artifacts/{body['artifact_id']}")
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    foreign = client.get(f"/v1/projects/{other.json()['id']}/artifacts/{body['artifact_id']}")
    assert foreign.status_code == 404
    headers = {**ORIGIN, "X-CSRF-Token": client.get("/v1/auth/session").json()["csrf_token"]}
    removed = client.delete(f"/v1/projects/{project_id}/artifacts/{body['artifact_id']}", headers=headers)
    assert removed.status_code == 204
    assert client.get(f"/v1/projects/{project_id}/artifacts/{body['artifact_id']}").status_code == 404
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    located = list(store.root.glob(f"{project_id}/*/*"))
    assert located
    assert all(path.parent.name == body["sha256"][:2] and path.name == body["sha256"] for path in located)


def test_media_and_size_are_checked(client: TestClient, api_db) -> None:
    owner_id, project_id, _headers = _project(client)
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    with pytest.raises(ArtifactError) as mismatch:
        store.put(owner_id=owner_id, project_id=project_id, data=b"not-json", media_type="application/json")
    assert mismatch.value.code == "media_mismatch"
    with pytest.raises(ArtifactError) as oversized:
        store._validate_bytes(b"x" * (MAX_UPLOAD_BYTES + 1), "text/plain")
    assert oversized.value.code == "size_rejected"


def test_crash_after_durable_write_recovers(client: TestClient, api_db) -> None:
    owner_id, project_id, _headers = _project(client)
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    staged = store.stage_without_commit(project_id=project_id, data=b"recovered text", media_type="text/plain")
    with pytest.raises(ArtifactError):
        store.get(owner_id=owner_id, project_id=project_id, artifact_id=staged["artifact_id"])
    restored = store.recover(owner_id=owner_id, project_id=project_id)
    assert restored == [staged["artifact_id"]]
    data, media_type = store.get(owner_id=owner_id, project_id=project_id, artifact_id=staged["artifact_id"])
    assert data == b"recovered text"
    assert media_type == "text/plain"
    assert store.recover(owner_id=owner_id, project_id=project_id) == []


def test_archive_rejects_escape_links_and_limits(client: TestClient, api_db) -> None:
    owner_id, project_id, _headers = _project(client)
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)

    def zip_with(name: str, content: bytes = b"data", mode: int | None = None) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, content)
        return buffer.getvalue()

    with pytest.raises(ArtifactError):
        store.import_archive(owner_id=owner_id, project_id=project_id, data=zip_with("../escape.txt"), media_type="application/zip")
    with pytest.raises(ArtifactError):
        store.import_archive(owner_id=owner_id, project_id=project_id, data=zip_with("/tmp/escape.txt"), media_type="application/zip")
    with pytest.raises(ArtifactError):
        store.import_archive(owner_id=owner_id, project_id=project_id, data=zip_with("link", mode=stat.S_IFLNK | 0o777), media_type="application/zip")

    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("zeros.bin", b"\0" * 5000)
    with pytest.raises(ArtifactError) as ratio:
        store.import_archive(owner_id=owner_id, project_id=project_id, data=bomb.getvalue(), media_type="application/zip", max_ratio=2)
    assert ratio.value.code == "ratio_rejected"

    hardlink = io.BytesIO()
    with tarfile.open(fileobj=hardlink, mode="w") as archive:
        payload = tarfile.TarInfo("file.txt")
        payload.size = 4
        archive.addfile(payload, io.BytesIO(b"data"))
        link = tarfile.TarInfo("hard.txt")
        link.type = tarfile.LNKTYPE
        link.linkname = "file.txt"
        archive.addfile(link)
    with pytest.raises(ArtifactError) as linked:
        store.import_archive(owner_id=owner_id, project_id=project_id, data=hardlink.getvalue(), media_type="application/x-tar")
    assert linked.value.code == "link_rejected"

    accepted = zip_with("notes/readme.txt", b"snapshot")
    stored = store.import_archive(owner_id=owner_id, project_id=project_id, data=accepted, media_type="application/zip")
    extracted = store.root / "snapshots" / project_id / str(stored["artifact_id"]) / "notes" / "readme.txt"
    assert extracted.read_bytes() == b"snapshot"
    assert Path.home().resolve() not in extracted.resolve().parents


def test_storage_root_cannot_be_host_home() -> None:
    with pytest.raises(ArtifactError) as rejected:
        assert_storage_root(Path.home() / "aether-artifacts")
    assert rejected.value.code == "host_home_rejected"
