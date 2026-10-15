"""Load BAAI/bge-m3 and encode one string on CPU. Prints measured dimension and snapshot revision."""

from __future__ import annotations

import json

from FlagEmbedding import BGEM3FlagModel
from huggingface_hub import HfApi, snapshot_download


def main() -> None:
    snapshot = snapshot_download("BAAI/bge-m3")
    info = HfApi().model_info("BAAI/bge-m3")
    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False, devices="cpu")
    encoded = model.encode(["aether probe"], return_dense=True, return_sparse=False, return_colbert_vecs=False)
    vector = encoded["dense_vecs"][0]
    print(
        json.dumps(
            {
                "embedding_revision": info.sha,
                "snapshot": snapshot,
                "dimension_probed": int(vector.shape[-1]),
                "weights_present": True,
                "device": "cpu",
                "sample_norm": float((vector ** 2).sum() ** 0.5),
            }
        )
    )


if __name__ == "__main__":
    main()
