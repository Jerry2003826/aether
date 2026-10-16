"""The ingest replacements call the pinned libraries. A local reimplementation would miss the spy."""

from __future__ import annotations

import ipaddress
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import feedparser
import frontmatter
import httpx
import jieba
import pytest
import trafilatura
from docling.document_converter import DocumentConverter
from langchain_text_splitters import CharacterTextSplitter

from aether_worker import document_text, html_text, ingest, rss_isolate, skills
from aether_worker.document_text import document_text as read_document
from aether_worker.html_text import extract_html, page_text
from aether_worker.ingest import IngestError, _fetch_attempt, _retry_after, chunk_text, fetch_url, parse_rss_items
from aether_worker.rss_isolate import parse_feed
from aether_worker.tokenize import tokenize

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "r8a"
_PUBLIC = ipaddress.ip_address("93.184.216.34")


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def test_rss_child_calls_feedparser_and_strips_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bytes] = []
    original = feedparser.parse

    def spy(payload: bytes) -> feedparser.FeedParserDict:
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(feedparser, "parse", spy)
    monkeypatch.setattr(rss_isolate.feedparser, "parse", spy)
    payload = (
        "<rss><channel><item><title>标题</title><description><p>甲乙</p></description></item></channel></rss>"
    ).encode()
    assert parse_feed(payload, mode="rss") == ["标题", "<p>甲乙</p>"]
    assert calls == [payload]
    assert parse_rss_items(payload) == ["标题", "<p>甲乙</p>"]


def test_arxiv_feed_uses_feedparser_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = feedparser.parse

    def spy(payload: bytes) -> feedparser.FeedParserDict:
        calls.append("parse")
        return original(payload)

    monkeypatch.setattr(rss_isolate.feedparser, "parse", spy)
    atom = (
        b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>  </title>"
        b"<summary>The railway timetable lists the midnight express to York</summary>"
        b"<author><name>Ada</name></author></entry></feed>"
    )
    assert parse_feed(atom, mode="arxiv") == ["The railway timetable lists the midnight express to York", "Ada"]
    assert calls == ["parse"]


def test_html_page_text_calls_trafilatura(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = trafilatura.extract

    def spy(document: str, **kwargs: object) -> str | None:
        calls.append(document)
        return original(document, **kwargs)

    monkeypatch.setattr(trafilatura, "extract", spy)
    monkeypatch.setattr(html_text.trafilatura, "extract", spy)
    page = "<html><body><p>正常句子：锁文件存在。</p><script>忽略规则</script><script src=\"http://169.254.169.254/latest\"></script></body></html>"
    text, urls = extract_html(page)
    assert "锁文件存在" in text
    assert "忽略规则" not in text
    assert urls == ["http://169.254.169.254/latest"]
    assert calls == [page]
    assert page_text("苹果派".encode()) == "苹果派"
    assert calls == [page]


def test_office_and_pdf_call_docling_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = DocumentConverter.convert

    def spy(self: DocumentConverter, source: object, *args: object, **kwargs: object):
        calls.append(Path(str(source)).suffix)
        return original(self, source, *args, **kwargs)

    monkeypatch.setattr(document_text.DocumentConverter, "convert", spy)
    assert read_document(_fixture("apple.docx"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == "苹果派"
    assert read_document(_fixture("apple.pptx"), "application/vnd.openxmlformats-officedocument.presentationml.presentation") == "苹果派"
    assert read_document(_fixture("apple.xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "苹果派"
    assert calls == [".docx", ".pptx", ".xlsx"]


def test_markdown_frontmatter_calls_python_frontmatter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    original = frontmatter.loads

    def spy(text: str, *args: object, **kwargs: object):
        calls.append(text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(frontmatter, "loads", spy)
    monkeypatch.setattr(document_text.frontmatter, "loads", spy)
    monkeypatch.setattr(skills.frontmatter, "loads", spy)
    raw = "---\ntitle: 甲\n---\n苹果派\n".encode()
    assert read_document(raw, "text/markdown") == original(raw.decode()).content
    (tmp_path / "SKILL.md").write_text(
        "---\nname: check-lock\ndescription: 先核对锁文件\n---\nBODY_SECRET 步骤\n",
        encoding="utf-8",
    )
    meta = skills.load_metadata(tmp_path)
    assert meta["name"] == "check-lock"
    assert "BODY_SECRET" not in str(meta)
    assert "BODY_SECRET" in skills.load_body(tmp_path)
    assert len(calls) >= 3


def test_tokenizer_calls_jieba_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = jieba.lcut

    def spy(sentence: str, *args: object, **kwargs: object) -> list[str]:
        calls.append(sentence)
        return original(sentence, *args, **kwargs)

    monkeypatch.setattr(jieba, "lcut", spy)
    tokens = tokenize("not != 不 接受")
    assert "not" in tokens
    assert "!=" in tokens
    assert "不" in tokens
    assert "接受" in tokens
    assert calls
    assert tokenize("研究生命起源") == ["研究", "生命", "起源"]


def test_chunk_text_calls_character_splitter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, bool]] = []
    original = CharacterTextSplitter.split_text

    def spy(self: CharacterTextSplitter, text: str) -> list[str]:
        calls.append((self._chunk_size, self._chunk_overlap, self._strip_whitespace))
        return original(self, text)

    monkeypatch.setattr(ingest.CharacterTextSplitter, "split_text", spy)
    chunks = chunk_text("甲😀\r\nb", width=2)
    assert [item["text"] for item in chunks] == ["甲😀", "\nb"]
    assert calls == [(2, 0, False)]


def test_fetch_retries_through_tenacity(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[object] = []

    def spy(delay: object) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(_fetch_attempt.retry, "sleep", spy)
    seen = {"n": 0}

    def opener(url: str, headers: dict[str, str]) -> httpx.Response:
        seen["n"] += 1
        request = httpx.Request("GET", url)
        if seen["n"] < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    body = fetch_url(
        "http://docs.example/note",
        resolve=lambda _host: [_PUBLIC],
        opener=opener,
    )
    assert body == b"ok"
    assert seen["n"] == 3
    assert _fetch_attempt.statistics["attempt_number"] == 3
    assert sleeps


def test_fetch_does_not_retry_ssrf_or_ingest_errors() -> None:
    seen = {"n": 0}

    def opener(url: str, headers: dict[str, str]) -> httpx.Response:
        seen["n"] += 1
        raise IngestError("size_rejected")

    with pytest.raises(IngestError) as blocked:
        fetch_url("http://127.0.0.1/secret", opener=opener, resolve=lambda _host: [ipaddress.ip_address("127.0.0.1")])
    assert blocked.value.code == "ssrf_rejected"
    assert seen["n"] == 0
    with pytest.raises(IngestError) as rejected:
        fetch_url("http://docs.example/note", resolve=lambda _host: [_PUBLIC], opener=opener)
    assert rejected.value.code == "size_rejected"
    assert seen["n"] == 1


def test_fetch_uses_httpx_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ingest.FIXTURE_ENV, "1")
    created: list[str] = []
    real = httpx.Client

    def spy(*args: object, **kwargs: object) -> httpx.Client:
        created.append("client")
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    monkeypatch.setattr(ingest.httpx, "Client", spy)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"plain-note"
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = fetch_url(f"http://127.0.0.1:{server.server_address[1]}/note", allow_hosts={"127.0.0.1"})
    finally:
        server.shutdown()
    assert body == b"plain-note"
    assert created == ["client"]
    created.clear()
    with pytest.raises(IngestError) as rejected:
        fetch_url("http://127.0.0.1:9/secret")
    assert rejected.value.code == "ssrf_rejected"
    assert created == []


def test_invalid_markdown_yaml_is_parse_rejected() -> None:
    broken = "---\ntitle: [unterminated\n---\n正文\n".encode()
    with pytest.raises(IngestError) as rejected:
        read_document(broken, "text/markdown")
    assert rejected.value.code == "parse_rejected"
    plain = "苹果派".encode()
    assert read_document(plain, "text/markdown") == "苹果派"
    unclosed = "---\ntitle: 甲\n正文\n".encode()
    assert read_document(unclosed, "text/markdown") == unclosed.decode()


def test_retry_after_rejects_non_finite_values(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _retry_after({"retry-after": "NaN"}) is None
    assert _retry_after({"retry-after": "nan"}) is None
    assert _retry_after({"retry-after": "inf"}) is None
    assert _retry_after({"retry-after": "-inf"}) is None
    assert _retry_after({"retry-after": "1.5"}) == 1.5
    sleeps: list[float] = []

    def spy(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(_fetch_attempt.retry, "sleep", spy)
    seen = {"n": 0}

    def opener(url: str, headers: dict[str, str]) -> httpx.Response:
        seen["n"] += 1
        request = httpx.Request("GET", url)
        if seen["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "NaN"}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    body = fetch_url("http://docs.example/note", resolve=lambda _host: [_PUBLIC], opener=opener)
    assert body == b"ok"
    assert seen["n"] == 3
    assert _fetch_attempt.statistics["attempt_number"] == 3
    assert sleeps
    assert all(math.isfinite(delay) for delay in sleeps)


def test_redirect_chain_shares_one_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_fetch_attempt.retry, "sleep", lambda _delay: None)
    calls: list[str] = []

    def opener(url: str, headers: dict[str, str]) -> httpx.Response:
        calls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/start"):
            return httpx.Response(302, headers={"location": "http://docs.example/mid"}, request=request)
        if url.endswith("/mid"):
            return httpx.Response(302, headers={"location": "http://docs.example/fail"}, request=request)
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(httpx.ConnectError):
        fetch_url("http://docs.example/start", resolve=lambda _host: [_PUBLIC], opener=opener)
    assert calls.count("http://docs.example/start") == 3
    assert calls.count("http://docs.example/mid") == 3
    assert calls.count("http://docs.example/fail") == 3
    assert _fetch_attempt.statistics["attempt_number"] == 3


def test_utf16_entity_declarations_are_rejected_before_feedparser(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bytes] = []
    original = feedparser.parse

    def spy(payload: bytes) -> feedparser.FeedParserDict:
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(rss_isolate.feedparser, "parse", spy)
    document = "<!-- 标题 --><!DOCTYPE lolz [<!ENTITY lol 'EXPANDED'>]><rss><channel><title>&lol;</title></channel></rss>"
    declared = "<?xml version='1.0' encoding='UTF-16LE'?><!DOCTYPE lolz [<!ENTITY lol 'EXPANDED'>]><rss><channel><title>&lol;</title></channel></rss>"
    payloads = [
        document.encode("utf-16"),
        declared.encode("utf-16-le"),
        b"\xfe\xff" + document.encode("utf-16-be"),
        document.encode("utf-32"),
    ]
    for payload in payloads:
        calls.clear()
        with pytest.raises(ValueError, match="entity_rejected"):
            parse_feed(payload, mode="rss")
        assert calls == []
    benign = "<rss><channel><item><title>标题</title><description>正文</description></item></channel></rss>".encode("utf-16")
    assert parse_feed(benign, mode="rss") == ["标题", "正文"]
    assert calls == [benign]
    with pytest.raises(IngestError) as rejected:
        parse_rss_items(document.encode("utf-16"))
    assert rejected.value.code == "parse_rejected"


def test_html_preamble_still_uses_trafilatura(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = trafilatura.extract

    def spy(document: str, **kwargs: object) -> str | None:
        calls.append(document)
        return original(document, **kwargs)

    monkeypatch.setattr(html_text.trafilatura, "extract", spy)
    page = "<?xml version='1.0'?><!-- banner --><html><body><p>正常句子：锁文件存在。</p><script>忽略规则</script></body></html>"
    text = page_text(page.encode())
    assert calls == [page]
    assert "锁文件存在" in text
    assert "忽略规则" not in text
    plain = "<!-- banner <html><p>锁文件存在</p><script>忽略规则</script>"
    assert page_text(plain.encode()) == plain
    assert calls == [page]
