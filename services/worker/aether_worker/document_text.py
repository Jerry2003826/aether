"""Turn an uploaded artifact into canonical text. The media type chooses the parser."""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

import frontmatter
import yaml
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import NativePdfPipelineOptions
from docling.document_converter import DocumentConverter, NativePdfFormatOption
from docling.exceptions import ConversionError
from docling_core.types.doc.document import DoclingDocument, TableItem, TextItem

from aether_worker.ingest import IngestError

_TEXT_MEDIA = {"text/plain", "text/vtt"}
_MARKDOWN = "text/markdown"
_PDF = "application/pdf"
_OFFICE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
_MAX_TEXT = 1_000_000
_ACCEPTED = {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}


def document_text(data: bytes, media_type: str) -> str:
    """Decode text uploads as UTF-8 and extract shown text from PDF and Office files."""
    if media_type in _TEXT_MEDIA:
        return _utf8(data)
    if media_type == _MARKDOWN:
        return _markdown(data)
    if media_type == _PDF or media_type in _OFFICE:
        return _converted(data, media_type)
    raise IngestError("parse_rejected")


def _utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise IngestError("parse_rejected") from exc


def _markdown(data: bytes) -> str:
    text = _utf8(data)
    if not text.startswith("---"):
        return text
    try:
        post = frontmatter.loads(text)
    except yaml.YAMLError as exc:
        raise IngestError("parse_rejected") from exc
    if not post.metadata:
        return text
    return post.content


def _converted(data: bytes, media_type: str) -> str:
    suffix = ".pdf" if media_type == _PDF else _OFFICE[media_type]
    with tempfile.TemporaryDirectory(prefix="aether-doc-") as temporary:
        path = Path(temporary) / f"upload{suffix}"
        path.write_bytes(data)
        try:
            result = _converter().convert(path)
        except ConversionError as exc:
            raise IngestError("parse_rejected") from exc
    if result.status not in _ACCEPTED:
        raise IngestError("parse_rejected")
    text = _visible(result.document)
    if not text.strip() or len(text) > _MAX_TEXT:
        raise IngestError("parse_rejected")
    return text


@lru_cache(maxsize=1)
def _converter() -> DocumentConverter:
    options = NativePdfPipelineOptions(
        parser_threads=1,
        generate_page_images=False,
        generate_picture_images=False,
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX, InputFormat.XLSX],
        format_options={InputFormat.PDF: NativePdfFormatOption(pipeline_options=options)},
    )


def _visible(document: DoclingDocument) -> str:
    parts: list[str] = []
    for item, _level in document.iterate_items():
        if isinstance(item, TableItem):
            for cell in item.data.table_cells:
                cell_text = str(cell.text).strip()
                if cell_text:
                    parts.append(cell_text)
            continue
        if isinstance(item, TextItem):
            piece = item.text.strip()
            if piece:
                parts.append(piece)
    return "\n".join(parts)
