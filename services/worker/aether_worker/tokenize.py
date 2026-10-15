"""Frozen jieba 0.42.1 dictionary plus English and code tokens.

The jieba package publishes source only. CI installs with ``uv sync --locked --no-build``,
so the segmenter reads the vendored dictionary instead of building that package.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

TOKENIZER_VERSION = "jieba-0.42.1-dict-forward-max+code-symbols"
_DICT = Path(__file__).resolve().parents[3] / "config" / "tokenizer" / "jieba-0.42.1.dict.txt"
_LATIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPERATORS = ("!=", "==", "<=", ">=", "&&", "||", "->", "::")


@lru_cache(maxsize=1)
def _dictionary() -> tuple[frozenset[str], int]:
    words: set[str] = set()
    longest = 1
    for line in _DICT.read_text(encoding="utf-8").splitlines():
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
