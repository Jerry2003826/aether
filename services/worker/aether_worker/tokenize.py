"""jieba 0.42.1 ``cut``, with English words and code operators kept intact.

The dictionary is the ``dict.txt`` shipped in the pinned wheel. CI installs with
``uv sync --locked --no-build`` and cannot build jieba's source distribution, so the
wheel lives at ``third_party/wheels``. Its sha256 is ``config/tokenizer/jieba-0.42.1.json``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import warnings
from functools import lru_cache
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"jieba(\.|$)")
import jieba

TOKENIZER_VERSION = "jieba-0.42.1-cut+code-symbols"
_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = json.loads((_ROOT / "config" / "tokenizer" / "jieba-0.42.1.json").read_text(encoding="utf-8"))
_DICT_SHA256 = str(_MANIFEST["dict_sha256"])
_CODE = re.compile(r"!=|==|<=|>=|&&|\|\||->|::|[A-Za-z_][A-Za-z0-9_]*")

jieba.setLogLevel(logging.ERROR)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_dict() -> Path:
    spec = importlib.util.find_spec("jieba")
    if spec is None or not spec.origin:
        raise RuntimeError("jieba dictionary missing")
    path = Path(spec.origin).resolve().parent / "dict.txt"
    if not path.is_file() or _sha256(path) != _DICT_SHA256:
        raise RuntimeError("jieba dictionary hash mismatch")
    return path


@lru_cache(maxsize=1)
def _ready() -> None:
    cache = _ROOT / ".cache" / "tokenizer"
    cache.mkdir(parents=True, exist_ok=True)
    jieba.dt.tmp_dir = str(cache)
    jieba.set_dictionary(str(_package_dict()))
    jieba.initialize()


def tokenize(value: str) -> list[str]:
    """jieba.cut on Chinese spans. Negation words and code operators stay tokens."""
    _ready()
    tokens: list[str] = []
    index = 0
    for match in _CODE.finditer(value):
        tokens.extend(_chinese(value[index : match.start()]))
        tokens.append(match.group())
        index = match.end()
    tokens.extend(_chinese(value[index:]))
    return tokens


def _chinese(segment: str) -> list[str]:
    if segment.strip() == "":
        return []
    return [piece for piece in jieba.lcut(segment) if piece.strip()]
