"""Measure BGE-M3 vectors and bge-reranker-v2-m3 scores for the retrieval fixtures.

Run from the repo root after the ML environment can see the Hugging Face weights:

    HF_HOME=/workspace/.cache/huggingface uv run --locked --no-build python scripts/measure_retrieval_models.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ML = ROOT / "environments" / "ml"
DESTINATION = ROOT / "config" / "retrieval" / "measured.json"

TEXTS = [
    "苹果派的烘烤温度是一百八十度",
    "The railway timetable lists the midnight express to York",
    "庆祝 😀 完成发布",
    "assert left != right && flag == true",
    "不 接受这个结论",
    "not ready for release",
    "独角兽望远镜只在第二代索引出现",
    "苹果派",
    "York",
    "😀",
    "!=",
    "不",
    "not",
    "独角兽",
    "zzzznotinvocab",
]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _run(script: str, payload: dict) -> dict:
    completed = subprocess.run(
        ["uv", "run", "--frozen", "python", script],
        cwd=ML,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr[-2000:] or completed.stdout[-2000:])
    return json.loads(completed.stdout)


def main() -> None:
    encoded = _run("encode_texts.py", {"texts": TEXTS})
    documents = TEXTS[:7]
    queries = TEXTS[7:]
    pairs = [[query, document] for query in queries for document in documents]
    ranked = _run("rerank_pairs.py", {"pairs": pairs})
    scores = {}
    for pair, score in zip(pairs, ranked["scores"], strict=True):
        scores[_sha(pair[0] + "\n" + pair[1])] = round(float(score), 8)
    payload = {
        "embedding_model_id": "BAAI/bge-m3",
        "embedding_revision": encoded["embedding_revision"],
        "dimension": encoded["dimension"],
        "reranker_model_id": "BAAI/bge-reranker-v2-m3",
        "reranker_revision": ranked["reranker_revision"],
        "vectors": {_sha(text): [round(float(value), 8) for value in vector] for text, vector in zip(TEXTS, encoded["vectors"], strict=True)},
        "rerank_scores": scores,
    }
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"embedding_revision": payload["embedding_revision"], "reranker_revision": payload["reranker_revision"], "vectors": len(payload["vectors"]), "scores": len(scores)}))


if __name__ == "__main__":
    main()
