"""Token hashing and constant-time comparison. These helpers do not authenticate a caller by themselves."""

from __future__ import annotations

import hashlib
import hmac


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hashes_match(stored_hash: str, presented: str) -> bool:
    return hmac.compare_digest(stored_hash, sha256_text(presented))
