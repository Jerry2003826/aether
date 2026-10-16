"""Encode texts with the local BAAI/bge-m3 weights. One process, CPU only.

The revision comes from the repo lock. This process does not call the Hub, so a
missing snapshot fails instead of downloading during an ingestion.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# FlagEmbedding reads these when it is imported. They stay at module level so a
# direct run cannot fall through to a Hub download.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from FlagEmbedding import BGEM3FlagModel

_LOCK = Path(__file__).resolve().parents[2] / "config" / "models.lock.json"


def main() -> None:
    request = json.loads(sys.stdin.read())
    texts = request["texts"]
    revision = str(json.loads(_LOCK.read_text(encoding="utf-8"))["ml"]["embedding_revision"])
    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False, devices="cpu")
    encoded = model.encode(texts, return_dense=True, return_sparse=False, return_colbert_vecs=False)
    vectors = [[float(value) for value in row] for row in encoded["dense_vecs"]]
    if any(len(vector) != 1024 for vector in vectors):
        raise SystemExit("measured dimension is not 1024")
    sys.stdout.write(json.dumps({"embedding_revision": revision, "dimension": 1024, "vectors": vectors}))


if __name__ == "__main__":
    main()
