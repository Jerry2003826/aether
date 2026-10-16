import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from aether_gateway.registry import ToolDenied, admit_tool, load_registry

from aether_worker import threat_browser
from aether_worker.ingest import FIXTURE_ENV
from aether_worker.security_regression import INJECTION
from aether_worker.threat_browser import (
    Switches,
    ThreatBrowserError,
    VisitRequest,
    accept_as_verification_evidence,
    model_payload,
    read_page,
    tool_calls_from_page,
)
from aether_worker.threat_pins import CHROMIUM_BROWSER_VERSION, PLAYWRIGHT_VERSION
from aether_worker.threat_visit import visit_threat_page

BROWSER = Switches(browser_enabled=True, tor_enabled=False, onion_enabled=False, owner_confirmed=False)
TOR = Switches(browser_enabled=True, tor_enabled=True, onion_enabled=True, owner_confirmed=True)
REGISTRY = Path("config/tools.example.yaml")


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


@pytest.fixture(autouse=True)
def _deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(threat_browser, "default_switches", lambda path=None: BROWSER)


def _approved(url: str, *, switches: Switches, hosts: set[str], proxy: str | None = None) -> VisitRequest:
    return VisitRequest(
        url=url,
        switches=switches,
        deployed=switches,
        allow_hosts=frozenset(hosts),
        runtime="playwright",
        fixture_proxy=proxy,
    )


def _allow(_request: VisitRequest, _digest: str) -> str:
    return "owner"


def _grant():
    used = {"value": False}

    def authorize(_request: VisitRequest, _digest: str) -> str:
        if used["value"]:
            raise ThreatBrowserError("approval_consumed")
        used["value"] = True
        return "owner"

    return authorize


def _public(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if host == "docs.example":
        return ipaddress.ip_address("93.184.216.34")
    return ipaddress.ip_address(host)


class _Socks:
    def __init__(self, routes: dict[str, tuple[str, int]]) -> None:
        self.routes = routes
        self.seen: list[tuple[str, int]] = []
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        self._socket.settimeout(0.2)
        self._stop = threading.Event()
        self.port = int(self._socket.getsockname()[1])
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._socket.close()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._socket.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            version, methods = _read(conn, 2)
            _read(conn, methods)
            if version != 5:
                return
            conn.sendall(b"\x05\x00")
            header = _read(conn, 4)
            host = _socks_host(conn, header[3])
            port = int.from_bytes(_read(conn, 2), "big")
            self.seen.append((host, port))
            upstream = self.routes.get(host.rstrip(".").lower())
            if upstream is None:
                conn.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            remote = socket.create_connection(upstream, timeout=5)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            _relay(conn, remote)
        except OSError:
            return
        finally:
            conn.close()


def _read(conn: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = conn.recv(size - len(chunks))
        if not part:
            raise OSError("short")
        chunks.extend(part)
    return bytes(chunks)


def _socks_host(conn: socket.socket, kind: int) -> str:
    if kind == 1:
        return socket.inet_ntoa(_read(conn, 4))
    if kind == 3:
        return _read(conn, _read(conn, 1)[0]).decode()
    if kind == 4:
        return socket.inet_ntop(socket.AF_INET6, _read(conn, 16))
    raise OSError("address")


def _relay(left: socket.socket, right: socket.socket) -> None:
    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            return

    threads = [threading.Thread(target=pump, args=(left, right), daemon=True), threading.Thread(target=pump, args=(right, left), daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)


def test_playwright_reads_with_javascript_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    hits = {"pixel": 0, "page": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/pixel"):
                hits["pixel"] += 1
                body = b"img"
            else:
                hits["page"] += 1
                body = (
                    b"<html><body><p>visible-text</p><noscript>noscript-text</noscript>"
                    b"<div style='display:none'>hidden-instruction</div>"
                    b"<script>document.body.textContent=String.fromCharCode(82,65,78)</script>"
                    b"<img src='/pixel.gif'></body></html>"
                )
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server, port = _serve(Handler)
    try:
        request = _approved(f"http://127.0.0.1:{port}/", switches=BROWSER, hosts={"127.0.0.1"})
        audit: list[dict[str, object]] = []
        document = visit_threat_page(request, audit=audit, authorize=_allow)
    finally:
        server.shutdown()
    assert document.playwright_version == PLAYWRIGHT_VERSION
    assert document.browser_version == CHROMIUM_BROWSER_VERSION
    assert "HeadlessChrome" in document.user_agent
    assert "visible-text" in document.text
    assert "noscript-text" in document.text
    assert "RAN" not in document.text
    assert "hidden-instruction" not in document.text
    assert hits["page"] == 1
    assert hits["pixel"] == 0
    assert audit[-1]["decision"] == "completed"


def test_child_frame_document_is_not_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    hits = {"page": 0, "frame": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/frame"):
                hits["frame"] += 1
                body = b"<html><body>frame-secret</body></html>"
            else:
                hits["page"] += 1
                body = b"<html><body><p>visible-text</p><iframe src='/frame'></iframe></body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server, port = _serve(Handler)
    try:
        request = _approved(f"http://127.0.0.1:{port}/page", switches=BROWSER, hosts={"127.0.0.1"})
        document = visit_threat_page(request, audit=[], authorize=_allow)
    finally:
        server.shutdown()
    assert "visible-text" in document.text
    assert "frame-secret" not in document.text
    assert hits["page"] == 1
    assert hits["frame"] == 0


def test_prompt_injection_stays_untrusted_and_is_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = f"<html><body><p>{INJECTION}</p><div style='display:none'>hidden-instruction</div></body></html>".encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server, port = _serve(Handler)
    try:
        request = _approved(f"http://127.0.0.1:{port}/", switches=BROWSER, hosts={"127.0.0.1"})
        document = visit_threat_page(request, audit=[], authorize=_allow)
    finally:
        server.shutdown()
    payload = model_payload(document)
    assert payload["channel"] == "untrusted_web"
    assert payload["admissible_evidence"] is False
    assert INJECTION in str(payload["quoted_text"])
    assert "hidden-instruction" not in str(payload["quoted_text"])
    assert tool_calls_from_page(payload) == []
    with pytest.raises(ThreatBrowserError) as rejected:
        accept_as_verification_evidence(document)
    assert rejected.value.code == "browser_not_evidence"
    with pytest.raises(ToolDenied):
        admit_tool(load_registry(REGISTRY), {"project_id": "project-1", "capabilities": ["knowledge:read"], "egress": []}, "policy.modify", {})


def test_ssrf_blocks_loopback_redirect_and_proxy_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    hits = {"secret": 0, "proxy": 0, "page": 0}

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/secret"):
                hits["secret"] += 1
            elif self.path.startswith("/start"):
                self.send_response(302)
                self.send_header("location", f"http://localhost:{port}/secret")
                self.end_headers()
                return
            else:
                hits["page"] += 1
            body = b"<html><body>visible-text</body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits["proxy"] += 1
            self.send_response(502)
            self.end_headers()

        def do_CONNECT(self) -> None:  # noqa: N802
            hits["proxy"] += 1
            self.send_response(502)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server, port = _serve(Page)
    proxy, proxy_port = _serve(Proxy)
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setenv("ALL_PROXY", proxy_url)
    try:
        with pytest.raises(ThreatBrowserError) as direct:
            read_page(f"http://127.0.0.1:{port}/secret", switches=BROWSER, allow_hosts=None, resolve=lambda host: [_public(host)], proxy=None)
        assert direct.value.code == "ssrf_rejected"
        assert hits["secret"] == 0
        request = _approved(f"http://127.0.0.1:{port}/start", switches=BROWSER, hosts={"127.0.0.1"})
        with pytest.raises(ThreatBrowserError) as redirected:
            visit_threat_page(request, audit=[], authorize=_allow)
        assert redirected.value.code == "ssrf_rejected"
        assert hits["secret"] == 0
        page = _approved(f"http://127.0.0.1:{port}/page", switches=BROWSER, hosts={"127.0.0.1"})
        document = visit_threat_page(page, audit=[], authorize=_allow)
    finally:
        server.shutdown()
        proxy.shutdown()
    assert "visible-text" in document.text
    assert hits["page"] == 1
    assert hits["proxy"] == 0


def test_malicious_download_is_not_stored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"MZ" + b"payload"
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("content-disposition", "attachment; filename=payload.exe")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server, port = _serve(Handler)
    try:
        request = _approved(f"http://127.0.0.1:{port}/payload.exe", switches=BROWSER, hosts={"127.0.0.1"})
        with pytest.raises(ThreatBrowserError) as rejected:
            visit_threat_page(request, audit=[], authorize=_allow)
    finally:
        server.shutdown()
    assert rejected.value.code == "download_rejected"
    assert list(tmp_path.iterdir()) == []


def test_tor_and_onion_use_the_fake_proxy_and_are_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    monkeypatch.setattr(threat_browser, "default_switches", lambda path=None: TOR)
    seen_resolve: list[str] = []

    def resolve(host: str):
        seen_resolve.append(host)
        return [_public(host)]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/onion"):
                body = "<html><body>onion-excerpt</body></html>".encode()
            else:
                body = "<html><body>proxy-excerpt</body></html>".encode()
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server, port = _serve(Handler)
    pinned = str(_public("docs.example"))
    socks = _Socks({pinned: ("127.0.0.1", port), "example.onion": ("127.0.0.1", port)})
    proxy = f"socks5://127.0.0.1:{socks.port}"
    try:
        clear = VisitRequest(
            url=f"http://docs.example:{port}/page",
            switches=TOR,
            deployed=TOR,
            allow_hosts=frozenset({"docs.example"}),
            runtime="playwright",
            fixture_proxy=proxy,
        )
        audit: list[dict[str, object]] = []
        tor_document = visit_threat_page(clear, audit=audit, resolve=resolve, authorize=_allow)
        onion = VisitRequest(
            url="http://example.onion/onion",
            switches=TOR,
            deployed=TOR,
            runtime="playwright",
            fixture_proxy=proxy,
        )
        before = len(seen_resolve)
        onion_document = visit_threat_page(onion, audit=audit, resolve=resolve, authorize=_allow)
    finally:
        server.shutdown()
        socks.close()
    assert "proxy-excerpt" in tor_document.text
    assert tor_document.source_label == "tor"
    assert (pinned, port) in socks.seen
    assert onion_document.source_label == "onion"
    assert "onion-excerpt" in onion_document.text
    assert seen_resolve[before:] == []
    assert any(host.endswith(".onion") for host, _tor_port in socks.seen)
    with pytest.raises(ThreatBrowserError) as tor_evidence:
        accept_as_verification_evidence(tor_document)
    assert tor_evidence.value.code == "tor_not_evidence"
    with pytest.raises(ThreatBrowserError) as onion_evidence:
        accept_as_verification_evidence(onion_document)
    assert onion_evidence.value.code == "tor_not_evidence"
    assert audit[-1]["source_label"] == "onion"
    assert audit[-1]["admissible_evidence"] is False


def test_each_visit_is_one_shot_and_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    cookies: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            cookies.append(self.headers.get("Cookie"))
            body = b"<html><body>visible-text</body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("set-cookie", "session=secret")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server, port = _serve(Handler)
    try:
        request = _approved(f"http://127.0.0.1:{port}/", switches=BROWSER, hosts={"127.0.0.1"})
        audit: list[dict[str, object]] = []
        first_grant = _grant()
        first = visit_threat_page(request, audit=audit, authorize=first_grant)
        second = visit_threat_page(request, audit=audit, authorize=_grant())
        with pytest.raises(ThreatBrowserError) as replayed:
            visit_threat_page(request, audit=audit, authorize=first_grant)
    finally:
        server.shutdown()
    assert "visible-text" in first.text and "visible-text" in second.text
    assert cookies == [None, None]
    assert replayed.value.code == "approval_consumed"
    assert audit[1]["previous_hash"] == audit[0]["event_hash"]
    assert audit[2]["decision"] == "approval_consumed"
    assert audit[0]["url_sha256"] != request.url
