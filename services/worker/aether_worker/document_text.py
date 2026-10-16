"""Turn an uploaded artifact into canonical text. The media type chooses the parser."""

from __future__ import annotations

import zlib

from aether_worker.ingest import IngestError

_TEXT_MEDIA = {"text/plain", "text/markdown", "text/vtt"}
_MAX_STREAMS = 64
_MAX_INFLATE = 8 * 1024 * 1024
_MAX_TEXT = 1_000_000
_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}


def document_text(data: bytes, media_type: str) -> str:
    """Decode text uploads as UTF-8 and extract shown text from a text PDF."""
    if media_type in _TEXT_MEDIA:
        try:
            return data.decode("utf-8")
        except UnicodeError as exc:
            raise IngestError("parse_rejected") from exc
    if media_type == "application/pdf":
        return _pdf_text(data)
    raise IngestError("parse_rejected")


def _pdf_text(data: bytes) -> str:
    if not data.startswith(b"%PDF-"):
        raise IngestError("parse_rejected")
    pieces: list[str] = []
    for header, raw in _streams(data):
        decoded = _decode_stream(header, raw)
        if decoded is None:
            continue
        pieces.extend(_shown_text(decoded))
        if sum(len(item) for item in pieces) > _MAX_TEXT:
            raise IngestError("parse_rejected")
    text = "\n".join(item for item in pieces if item.strip())
    if not text.strip() or len(text) > _MAX_TEXT:
        raise IngestError("parse_rejected")
    return text


def _streams(data: bytes) -> list[tuple[bytes, bytes]]:
    found: list[tuple[bytes, bytes]] = []
    cursor = 0
    while True:
        start = _find_stream(data, cursor)
        if start < 0:
            break
        if len(found) >= _MAX_STREAMS:
            raise IngestError("parse_rejected")
        header = data[max(0, start - 800) : start]
        body = _stream_body(data, start + len(b"stream"))
        length = _direct_length(header)
        if length is None:
            end = data.find(b"endstream", body)
            if end < 0:
                raise IngestError("parse_rejected")
            raw = data[body:end]
            if raw.endswith(b"\r\n"):
                raw = raw[:-2]
            elif raw.endswith((b"\n", b"\r")):
                raw = raw[:-1]
            cursor = end + len(b"endstream")
        else:
            if body + length > len(data):
                raise IngestError("parse_rejected")
            raw = data[body : body + length]
            cursor = body + length
        found.append((header, raw))
    return found


def _direct_length(header: bytes) -> int | None:
    at = header.rfind(b"/Length")
    if at < 0:
        return None
    rest = header[at + len(b"/Length") :].lstrip(b" \t\r\n")
    digits = bytearray()
    index = 0
    while index < len(rest) and rest[index : index + 1] in b"0123456789" and len(digits) < 9:
        digits.append(rest[index])
        index += 1
    if not digits:
        return None
    tail = rest[index:].lstrip(b" \t\r\n")
    if tail.startswith(b"0 R") or tail.startswith(b"R"):
        return None
    return int(digits)


def _find_stream(data: bytes, cursor: int) -> int:
    while True:
        start = data.find(b"stream", cursor)
        if start < 0:
            return -1
        if data[start - 3 : start] == b"end":
            cursor = start + len(b"stream")
            continue
        return start


def _stream_body(data: bytes, body: int) -> int:
    if data[body : body + 2] == b"\r\n":
        return body + 2
    if body < len(data) and data[body : body + 1] in b"\r\n":
        return body + 1
    return body


def _decode_stream(header: bytes, raw: bytes) -> bytes | None:
    if b"/FlateDecode" in header:
        return _inflate(raw)
    if b"/Filter" in header:
        return None
    return raw


def _inflate(raw: bytes) -> bytes | None:
    for wbits in (15, -15):
        try:
            obj = zlib.decompressobj(wbits)
            out = obj.decompress(raw, _MAX_INFLATE)
        except zlib.error:
            continue
        if obj.unconsumed_tail or not obj.eof:
            return None
        return out
    return None


def _shown_text(blob: bytes) -> list[str]:
    texts: list[str] = []
    index = 0
    while index < len(blob):
        kind = blob[index]
        if kind == 0x28:
            parsed = _read_literal(blob, index)
            if parsed is None:
                index += 1
                continue
            raw, index = parsed
            if _operator(blob, index) in {b"Tj", b"'"}:
                texts.append(_decode_pdf_text(raw))
            continue
        if kind == 0x5B:
            parts, index = _read_array(blob, index)
            if _operator(blob, index) == b"TJ":
                texts.extend(parts)
            continue
        if kind == 0x3C and not blob.startswith(b"<<", index):
            raw, index = _read_hex(blob, index)
            if raw and _operator(blob, index) in {b"Tj", b"'"}:
                texts.append(_decode_pdf_text(raw))
            continue
        index += 1
    return texts


def _operator(blob: bytes, index: int) -> bytes:
    while index < len(blob) and blob[index] in b" \t\r\n":
        index += 1
    end = index
    while end < len(blob) and blob[end] not in b" \t\r\n[]()<>/":
        end += 1
    return blob[index:end]


def _read_literal(data: bytes, start: int) -> tuple[bytes, int] | None:
    if start >= len(data) or data[start] != 0x28:
        return None
    out = bytearray()
    depth = 1
    index = start + 1
    while index < len(data):
        byte = data[index]
        if byte == 0x5C:
            if index + 1 >= len(data):
                return None
            nxt = data[index + 1]
            mapped = _ESCAPES.get(nxt)
            if mapped is not None:
                out.append(mapped)
                index += 2
                continue
            if 48 <= nxt <= 55:
                digits = bytearray()
                cursor = index + 1
                while cursor < len(data) and len(digits) < 3 and 48 <= data[cursor] <= 55:
                    digits.append(data[cursor])
                    cursor += 1
                out.append(int(digits, 8) & 0xFF)
                index = cursor
                continue
            out.append(nxt)
            index += 2
            continue
        if byte == 0x28:
            depth += 1
            out.append(byte)
        elif byte == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), index + 1
            out.append(byte)
        else:
            out.append(byte)
        index += 1
    return None


def _read_array(blob: bytes, start: int) -> tuple[list[str], int]:
    texts: list[str] = []
    index = start + 1
    while index < len(blob):
        if blob[index] == 0x5D:
            return texts, index + 1
        if blob[index] == 0x28:
            parsed = _read_literal(blob, index)
            if parsed is None:
                return texts, index + 1
            raw, index = parsed
            texts.append(_decode_pdf_text(raw))
            continue
        if blob[index] == 0x3C and not blob.startswith(b"<<", index):
            raw, index = _read_hex(blob, index)
            if raw:
                texts.append(_decode_pdf_text(raw))
            continue
        index += 1
    return texts, index


def _read_hex(blob: bytes, start: int) -> tuple[bytes, int]:
    end = blob.find(b">", start + 1)
    if end < 0:
        return b"", start + 1
    digits = bytes(byte for byte in blob[start + 1 : end] if byte not in b" \t\r\n")
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii")), end + 1
    except ValueError:
        return b"", end + 1


def _decode_pdf_text(raw: bytes) -> str:
    try:
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be")
        if raw.startswith(b"\xff\xfe"):
            return raw[2:].decode("utf-16-le")
        return raw.decode("utf-8")
    except UnicodeError:
        return raw.decode("latin-1")
