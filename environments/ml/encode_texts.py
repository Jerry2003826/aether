"""Encode texts with the local BAAI/bge-m3 weights. One process, CPU only."""

from __future__ import annotations

import json
import sys

from FlagEmbedding import BGEM3FlagModel
from huggingface_hub import HfApi


def main() -> None:
    request = json.loads(sys.stdin.read())
    texts = request["texts"]
    info = HfApi().model_info("BAAI/bge-m3")
    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False, devices="cpu")
    encoded = model.encode(texts, return_dense=True, return_sparse=False, return_colbert_vecs=False)
    vectors = [[float(value) for value in row] for row in encoded["dense_vecs"]]
    if any(len(vector) != 1024 for vector in vectors):
        raise SystemExit("measured dimension is not 1024")
    sys.stdout.write(json.dumps({"embedding_revision": info.sha, "dimension": 1024, "vectors": vectors}))


if __name__ == "__main__":
    main()
