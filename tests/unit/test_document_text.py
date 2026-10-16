import uuid
import zlib

import pytest

from aether_worker.document_text import document_text
from aether_worker.ingest import IngestError
from aether_worker.pending_jobs import _read_body


def _pdf(shown: bytes, *, flate: bool) -> bytes:
    content = b"BT (" + shown + b") Tj ET"
    header = ""
    if flate:
        content = zlib.compress(content)
        header = "/Filter /FlateDecode "
    stream = f"<< {header}/Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
    return b"%PDF-1.4\n1 0 obj\n" + stream + b"\nendobj\n%%EOF"


def test_text_pdf_extracts_shown_text_and_ignores_the_container() -> None:
    plain = _pdf("苹果派".encode(), flate=False)
    packed = _pdf("苹果派".encode(), flate=True)
    assert document_text(plain, "application/pdf") == "苹果派"
    assert document_text(packed, "application/pdf") == "苹果派"
    assert "%PDF" not in document_text(plain, "application/pdf")
    escaped = _pdf(br"a\(b\) \101", flate=False)
    assert document_text(escaped, "application/pdf") == "a(b) A"


def test_markdown_decodes_and_non_text_media_is_rejected() -> None:
    assert document_text("苹果派".encode(), "text/markdown") == "苹果派"
    with pytest.raises(IngestError) as rejected:
        document_text(b"PK\x03\x04not-a-document", "application/zip")
    assert rejected.value.code == "parse_rejected"
    with pytest.raises(IngestError) as broken:
        document_text(b"%PDF-1.4\nno shown text\n", "application/pdf")
    assert broken.value.code == "parse_rejected"


def test_local_document_uses_the_artifact_media_type() -> None:
    payload = _pdf("苹果派".encode(), flate=True)

    class _Store:
        def get(self, **_kwargs):
            return payload, "application/pdf"

    raw, text, media_type = _read_body(
        {"kind": "local_document", "locator": str(uuid.uuid4())},
        store=_Store(),
        owner_id="owner",
        project_id="project",
    )
    assert raw == payload
    assert media_type == "application/pdf"
    assert text == "苹果派"
    assert "%PDF" not in text
