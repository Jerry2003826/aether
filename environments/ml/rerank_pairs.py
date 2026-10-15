"""Score query/passage pairs with the local BAAI/bge-reranker-v2-m3 weights."""

from __future__ import annotations

import json
import sys

from FlagEmbedding import FlagReranker
from huggingface_hub import HfApi


def main() -> None:
    request = json.loads(sys.stdin.read())
    pairs = request["pairs"]
    info = HfApi().model_info("BAAI/bge-reranker-v2-m3")
    reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=False, devices="cpu")
    raw = reranker.compute_score(pairs, normalize=True)
    scores = [float(raw)] if isinstance(raw, float) else [float(item) for item in raw]
    if len(scores) != len(pairs):
        raise SystemExit("reranker returned a different number of scores")
    sys.stdout.write(json.dumps({"reranker_revision": info.sha, "scores": scores}))


if __name__ == "__main__":
    main()
