"""Local TLS pages for the container test. It listens only when the fixture switch is on."""

from __future__ import annotations

import json
import socket
import ssl
import threading
from pathlib import Path

_ROOT = Path("/var/lib/tor/fixture")
_HITS_PATH = _ROOT / "hits.json"
_HITS = {"page": 0, "pixel": 0, "file": 0}
_LOCK = threading.Lock()
_PAGE = (
    b"<html><body><p>visible-text</p><noscript>noscript-text</noscript>"
    b"<div style='display:none'>hidden-instruction</div>"
    b"<script>document.body.textContent=String.fromCharCode(82,65,78)</script>"
    b"<img src='/pixel.gif'></body></html>"
)


def main() -> None:
    _ROOT.mkdir(mode=0o700, exist_ok=True)
    context, listener = _listener(8080, _ROOT / "cert.pem", _ROOT / "key.pem")
    while True:
        connection, _address = listener.accept()
        threading.Thread(target=_respond, args=(context, connection), daemon=True).start()


def _listener(port: int, certificate: Path, key: Path) -> tuple[ssl.SSLContext, socket.socket]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("", port))
    listener.listen()
    return context, listener


def _respond(context: ssl.SSLContext, connection: socket.socket) -> None:
    try:
        tls = context.wrap_socket(connection, server_side=True)
    except (ssl.SSLError, OSError):
        connection.close()
        return
    try:
        path = _request_path(_read_headers(tls))
        if path.startswith("/pixel"):
            _mark("pixel")
            _send(tls, b"img", "image/gif", None)
            return
        if path.startswith("/payload"):
            _mark("file")
            _send(tls, b"MZ" + b"payload", "application/octet-stream", "attachment; filename=payload.exe")
            return
        _mark("page")
        _send(tls, _PAGE, "text/html", None)
    finally:
        tls.close()


def _read_headers(connection: ssl.SSLSocket) -> bytes:
    raw = bytearray()
    while b"\r\n\r\n" not in raw and len(raw) < 8192:
        part = connection.recv(4096)
        if not part:
            break
        raw.extend(part)
    return bytes(raw)


def _request_path(raw: bytes) -> str:
    parts = raw.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2:
        return "/"
    return parts[1].decode("ascii", errors="replace")


def _send(connection: ssl.SSLSocket, body: bytes, media: str, disposition: str | None) -> None:
    headers = ["HTTP/1.1 200 OK", f"content-type: {media}", f"content-length: {len(body)}"]
    if disposition is not None:
        headers.append(f"content-disposition: {disposition}")
    payload = "\r\n".join([*headers, "", ""]).encode("ascii") + body
    connection.sendall(payload)


def _mark(name: str) -> None:
    with _LOCK:
        _HITS[name] += 1
        _HITS_PATH.write_text(json.dumps(_HITS), encoding="utf-8")


if __name__ == "__main__":
    main()
