"""Frozen jieba 0.42.1 dictionary plus English and code tokens.

The dictionary is jieba's own file. CI installs with ``uv sync --locked --no-build``,
which cannot build jieba's source distribution, so a missing package copy is downloaded
and checked against the sha256 in ``config/tokenizer/jieba-0.42.1.json``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

TOKENIZER_VERSION = "jieba-0.42.1-dict-forward-max+code-symbols"
_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = json.loads((_ROOT / "config" / "tokenizer" / "jieba-0.42.1.json").read_text(encoding="utf-8"))
_DICT_SHA256 = str(_MANIFEST["dict_sha256"])
_CACHE = _ROOT / ".cache" / "tokenizer" / "jieba-0.42.1.dict.txt"
_LATIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPERATORS = ("!=", "==", "<=", ">=", "&&", "||", "->", "::")


def _download_dictionary() -> None:
    last: Exception | None = None
    for _ in range(3):
        try:
            urllib.request.urlretrieve(str(_MANIFEST["dict_url"]), _CACHE)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            _CACHE.unlink(missing_ok=True)
    raise RuntimeError("jieba dictionary download failed") from last


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_dict() -> Path | None:
    spec = importlib.util.find_spec("jieba")
    if spec is None or not spec.origin:
        return None
    path = Path(spec.origin).resolve().parent / "dict.txt"
    return path if path.is_file() else None


def _dictionary_path() -> Path:
    for candidate in (_package_dict(), _CACHE):
        if candidate is not None and candidate.is_file() and _sha256(candidate) == _DICT_SHA256:
            return candidate
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _download_dictionary()
    if _sha256(_CACHE) != _DICT_SHA256:
        _CACHE.unlink(missing_ok=True)
        raise RuntimeError("jieba dictionary hash mismatch")
    return _CACHE


@lru_cache(maxsize=1)
def _dictionary() -> tuple[frozenset[str], int]:
    words: set[str] = set()
    longest = 1
    for line in _dictionary_path().read_text(encoding="utf-8").splitlines():
        word = line.split(" ", 1)[0].strip()
        if not word:
            continue
        words.add(word)
        longest = max(longest, len(word))
    return frozenset(words), longest


def tokenize(value: str) -> list[str]:
    """Longest dictionary match. Negation words and code operators stay tokens."""
    words, longest = _dictionary()
    tokens: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        if value[index].isspace():
            index += 1
            continue
        operator = next((item for item in _OPERATORS if value.startswith(item, index)), None)
        if operator is not None:
            tokens.append(operator)
            index += len(operator)
            continue
        if value[index].isascii() and (value[index].isalnum() or value[index] == "_"):
            match = _LATIN.match(value, index)
            if match is not None:
                tokens.append(match.group())
                index = match.end()
                continue
        limit = min(longest, length - index)
        matched = ""
        for size in range(limit, 0, -1):
            piece = value[index : index + size]
            if piece in words:
                matched = piece
                break
        if matched:
            tokens.append(matched)
            index += len(matched)
            continue
        tokens.append(value[index])
        index += 1
    return tokens
