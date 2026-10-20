"""The Hermes archive download retries transient failures with a fresh request each time (CI run 39232019435).

CI failed twice at `install_hermes.py` with `HTTP Error 302: The HTTP server returned a redirect error that
would lead to an infinite loop`, 39 s into the step: the 2+4+8+16 s of backoff of four failed attempts.
github.com answers the archive URL with one 302 to codeload.github.com. _download built one
urllib Request and reused it for every attempt, and urllib's redirect handler keeps its loop count on
that request (`redirect_dict`): each attempt that was redirected and then failed transiently added one
visit to the codeload URL, so the fifth attempt hit `max_repeats` (4) and urllib raised a 302 instead of
downloading, hiding the real error. Body read errors (a reset or short body) were not retried at all.

Now every attempt uses a new Request; HTTP 429/5xx, a redirect loop, connection errors, timeouts and a
short body are retried (5 attempts, 2/4/8/16 s backoff); anything else fails at once; the last failure is
an explicit SystemExit naming the attempts and the last reason. The archive checksum is still checked
after the download and a mismatch is never retried.
"""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_hermes.py"
_PAYLOAD = b"pinned hermes archive bytes for the download test"


def _load():
    spec = importlib.util.spec_from_file_location("install_hermes_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Codeload:
    """github.com-like /archive (one 302) and codeload-like /codeload answering from a script of replies."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.archive_hits = 0
        self.codeload_hits = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/archive":
                    owner.archive_hits += 1
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/codeload")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                owner.codeload_hits += 1
                reply = owner.replies.pop(0) if len(owner.replies) > 1 else owner.replies[0]
                if reply == "ok":
                    self._send(200, _PAYLOAD)
                elif reply == "short":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(_PAYLOAD)))
                    self.end_headers()
                    self.wfile.write(_PAYLOAD[:10])
                    self.wfile.flush()
                    self.close_connection = True
                elif reply == "loop":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/codeload")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send(int(reply), b"busy")

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/archive"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def installer(monkeypatch):
    module = _load()
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    module.sleeps = sleeps
    return module


@pytest.fixture
def codeload():
    made: list[_Codeload] = []

    def start(replies: list[str]) -> _Codeload:
        server = _Codeload(replies)
        made.append(server)
        return server

    yield start
    for server in made:
        server.close()


def test_four_redirected_transient_failures_then_success_downloads(installer, codeload, tmp_path) -> None:
    server = codeload(["503", "502", "503", "504", "ok"])
    target = tmp_path / "hermes.tar.gz"
    installer._download(server.url, target)
    assert target.read_bytes() == _PAYLOAD
    assert server.archive_hits == 5 and server.codeload_hits == 5
    assert installer.sleeps == [2, 4, 8, 16]


def test_a_short_body_is_transient_and_retried(installer, codeload, tmp_path) -> None:
    server = codeload(["short", "ok"])
    target = tmp_path / "hermes.tar.gz"
    installer._download(server.url, target)
    assert target.read_bytes() == _PAYLOAD
    assert not target.with_suffix(target.suffix + ".partial").exists()
    assert installer.sleeps == [2]


def test_a_redirect_loop_is_transient_and_retried(installer, codeload, tmp_path) -> None:
    server = codeload(["loop", "loop", "loop", "loop", "loop", "ok"])
    target = tmp_path / "hermes.tar.gz"
    installer._download(server.url, target)
    assert target.read_bytes() == _PAYLOAD
    assert installer.sleeps == [2]


def test_a_persistent_transient_failure_fails_explicitly_after_the_last_attempt(installer, codeload, tmp_path) -> None:
    server = codeload(["503"])
    target = tmp_path / "hermes.tar.gz"
    with pytest.raises(SystemExit) as raised:
        installer._download(server.url, target)
    message = str(raised.value)
    assert "hermes archive download failed after 5 attempts" in message
    assert "HTTP 503" in message
    assert "302" not in message
    assert server.codeload_hits == 5
    assert installer.sleeps == [2, 4, 8, 16]
    assert not target.exists() and not target.with_suffix(target.suffix + ".partial").exists()


def test_a_permanent_http_error_is_not_retried(installer, codeload, tmp_path) -> None:
    server = codeload(["404"])
    with pytest.raises(SystemExit) as raised:
        installer._download(server.url, tmp_path / "hermes.tar.gz")
    assert "hermes archive download failed after 1 attempt" in str(raised.value)
    assert "HTTP 404" in str(raised.value)
    assert server.codeload_hits == 1
    assert installer.sleeps == []


def test_a_checksum_mismatch_is_never_retried(installer, codeload, tmp_path, monkeypatch) -> None:
    server = codeload(["ok"])
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setattr(installer, "ARCHIVE_URL", server.url)
    monkeypatch.setattr(installer, "_lock_matches", lambda _lock: True)
    with pytest.raises(SystemExit) as raised:
        installer.install()
    assert "hermes archive sha256" in str(raised.value)
    assert server.codeload_hits == 1
    assert installer.sleeps == []
    assert not (tmp_path / f".hermes-v{installer.VERSION}.tar.gz").exists()
