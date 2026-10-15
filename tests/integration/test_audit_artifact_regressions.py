"""Repository-native regressions for audit b570f91.

Copy into tests/integration and run with real PostgreSQL fixtures. These tests
were written during the audit but NOT executed in the audit environment.
The small binary-upload patch is not intended to fix all the failures here.
"""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from aether_storage.artifacts import ArtifactStore
from tests.integration.test_artifacts import _project


def _zip() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('data.bin', bytes(range(256)))
    return stream.getvalue()


def _pdf() -> bytes:
    # Minimal one-page PDF, including the standard non-ASCII binary comment.
    body = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>',
    ]
    offsets = []
    for index, item in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f'{index} 0 obj\n'.encode() + item + b'\nendobj\n')
    start = len(body)
    body.extend(b'xref\n0 4\n0000000000 65535 f \n')
    for offset in offsets:
        body.extend(f'{offset:010d} 00000 n \n'.encode())
    body.extend(f'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return bytes(body)


@pytest.mark.parametrize('name,media,payload', [
    ('document.pdf', 'application/pdf', _pdf()),
    ('snapshot.zip', 'application/zip', _zip()),
], ids=['pdf', 'zip'])
def test_binary_upload_roundtrip_and_idempotency(client: TestClient, api_db, name, media, payload):
    _owner, project, headers = _project(client)
    url = f'/v1/projects/{project}/artifacts'
    request_headers = {**headers, 'Idempotency-Key': 'audit-binary-roundtrip'}
    response = client.post(url, headers=request_headers, files={'file': (name, payload, media)})
    assert response.status_code == 201, response.text
    stored = response.json()
    assert stored['sha256'] == hashlib.sha256(payload).hexdigest()
    assert stored['byte_size'] == len(payload)
    assert stored['media_type'] == media
    replay = client.post(url, headers=request_headers, files={'file': (name, payload, media)})
    assert replay.status_code == 201, replay.text
    assert replay.json()['artifact_id'] == stored['artifact_id']
    downloaded = client.get(f'{url}/{stored["artifact_id"]}')
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    conflict = client.post(url, headers=request_headers, files={'file': (name, payload + b'\n', media)})
    assert conflict.status_code == 409, conflict.text


def test_invalid_utf8_text_is_rejected_without_server_error(client: TestClient, api_db):
    _owner, project, headers = _project(client)
    response = client.post(f'/v1/projects/{project}/artifacts',
        headers={**headers, 'Idempotency-Key': 'audit-invalid-utf8'},
        files={'file': ('invalid.txt', b'\xff', 'text/plain')})
    assert response.status_code == 422, response.text
    assert response.json()['code'] == 'media_mismatch'


def test_external_transaction_must_not_write_committed_journal_early(client: TestClient, api_db):
    owner, project, _headers = _project(client)
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    with api_db.transaction(owner_id=owner, project_id=project) as conn:
        stored = store.put(owner_id=owner, project_id=project, data=b'outer-transaction-audit',
                           media_type='text/plain', conn=conn)
        # The outer transaction has NOT committed yet.
        records = store._journal_rows(project)
        assert not any(r.get('state') == 'committed' and r.get('artifact_id') == stored['artifact_id']
                       for r in records)


def test_dedup_recovery_only_returns_existing_ids(client: TestClient, api_db):
    owner, project, _headers = _project(client)
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    args = dict(owner_id=owner, project_id=project, data=b'dedup-recovery-audit', media_type='text/plain')
    first = store.put(**args)
    second = store.put(**args)
    assert first['artifact_id'] == second['artifact_id']
    for artifact_id in store.recover(owner_id=owner, project_id=project):
        assert store.get(owner_id=owner, project_id=project, artifact_id=artifact_id) == (args['data'], 'text/plain')
    assert store.recover(owner_id=owner, project_id=project) == []


def test_same_archive_retry_keeps_a_complete_snapshot(client: TestClient, api_db):
    owner, project, _headers = _project(client)
    store = ArtifactStore(client.app.state.settings.artifact_root, api_db)
    args = dict(owner_id=owner, project_id=project, data=_zip(), media_type='application/zip')
    first = store.import_archive(**args)
    second = store.import_archive(**args)
    assert first['artifact_id'] == second['artifact_id']
    assert (store.root / 'snapshots' / project / first['artifact_id'] / 'data.bin').read_bytes() == bytes(range(256))
