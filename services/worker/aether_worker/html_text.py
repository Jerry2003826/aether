"""Visible page text. The transport stays in the fetch client; this module only reads bytes."""

from __future__ import annotations

import re

import trafilatura
from lxml import html as lxml_html
from lxml.etree import ParserError, XMLSyntaxError

from aether_worker.ingest import IngestError

_HTML_START = re.compile(
    rb"(?is)\s*(?:<!doctype\s+html\b|<html\b|<head\b|<body\b|<p\b|<div\b|<article\b|<main\b|<section\b|<h[1-6]\b|<ul\b|<ol\b|<table\b)"
)


def page_text(data: bytes) -> str:
    """Keep plain responses as UTF-8. HTML responses become trafilatura's main text."""
    if not _looks_like_html(data):
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise IngestError("parse_rejected") from exc
        if text.strip() == "":
            raise IngestError("empty_body")
        return text
    try:
        decoded = data.decode("utf-8")
    except UnicodeError as exc:
        raise IngestError("parse_rejected") from exc
    extracted = trafilatura.extract(decoded, include_comments=False, include_links=False)
    if extracted is None or extracted.strip() == "":
        raise IngestError("empty_body")
    return extracted


def extract_html(document: str) -> tuple[str, list[str]]:
    """Main text from trafilatura, plus script sources that the extractor drops."""
    extracted = trafilatura.extract(document, include_comments=False, include_links=False) or ""
    return extracted, _script_sources(document)


def _looks_like_html(data: bytes) -> bool:
    return _HTML_START.match(data.lstrip(b"\xef\xbb\xbf")) is not None


def _script_sources(document: str) -> list[str]:
    try:
        root = lxml_html.fromstring(document)
    except (ParserError, XMLSyntaxError, ValueError, TypeError):
        return []
    urls: list[str] = []
    for src in root.xpath("//script/@src"):
        if isinstance(src, str) and src:
            urls.append(src)
    return urls
