import json

import bm25s
import pytest

from aether_worker.retrieval import (
    DIMENSION,
    EMBEDDING_REVISION,
    RetrievalError,
    _bounded_lexical,
    _index_bm25,
    _lexical_ids,
    encode_texts,
    group_metrics,
    rrf,
)
from aether_worker.tokenize import tokenize


def test_tokenizer_keeps_negation_and_code_symbols() -> None:
    tokens = tokenize("not != 不 接受")
    assert "not" in tokens
    assert "!=" in tokens
    assert "不" in tokens
    assert "接受" in tokens


def test_rrf_counts_a_repeated_id_once_from_rank_one() -> None:
    fused = rrf([["a", "a", "b"], ["b"]])
    assert fused == ["b", "a"]
    only = rrf([["a", "b"]])
    assert only[0] == "a"
    assert rrf([[]]) == []


def test_filtered_lexical_does_not_rank_the_whole_corpus(tmp_path, monkeypatch) -> None:
    strong = "applepie " * 6
    texts = [strong] * 220 + ["applepie"]
    directory = tmp_path / "filtered"
    _index_bm25(texts, directory)
    ids = [f"chunk-{index}" for index in range(len(texts))]
    calls: list[int] = []
    original = bm25s.BM25.retrieve

    def wrapped(self, *args, **kwargs):
        if "k" in kwargs:
            calls.append(int(kwargs["k"]))
        elif len(args) >= 2 and isinstance(args[1], int):
            calls.append(int(args[1]))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(bm25s.BM25, "retrieve", wrapped)
    found = _bounded_lexical(directory, ids, "applepie", {ids[-1]: {"id": ids[-1]}}, wide=True)
    assert found == [ids[-1]]
    assert calls == []
    window = _lexical_ids(directory, ids, "applepie")
    assert ids[-1] not in window


def test_unknown_query_token_returns_no_lexical_hit(tmp_path) -> None:
    directory = tmp_path / "index"
    _index_bm25(["assert left != right"], directory)
    assert _lexical_ids(directory, ["chunk-1"], "zzzznotinvocab") == []
    assert _lexical_ids(directory, ["chunk-1"], "!=") == ["chunk-1"]


def test_group_metrics_score_each_channel() -> None:
    metrics = group_metrics({"bm25": ["a", "b"], "dense": ["b"], "rrf": ["a"], "rerank": ["a"]}, {"a"})
    assert metrics["bm25"]["recall_at_10"] == 1
    assert metrics["dense"]["mrr_at_10"] == 0
    assert metrics["rerank"]["ndcg_at_10"] == 1


def test_wrong_embedding_revision_is_rejected(tmp_path, monkeypatch) -> None:
    path = tmp_path / "measured.json"
    path.write_text(json.dumps({"embedding_revision": "0" * 40, "dimension": DIMENSION, "vectors": {}, "rerank_scores": {}}), encoding="utf-8")
    monkeypatch.setattr("aether_worker.retrieval.MEASUREMENTS", path)
    monkeypatch.setattr("aether_worker.retrieval._CACHE", None)
    with pytest.raises(RetrievalError) as rejected:
        encode_texts(["苹果派"])
    assert rejected.value.code == "revision_mismatch"


def test_cache_miss_does_not_invent_a_vector(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("model process is not available")

    monkeypatch.setattr("aether_worker.retrieval.subprocess.run", boom)
    with pytest.raises(RetrievalError) as rejected:
        encode_texts(["this sentence is not in the measured table"])
    assert rejected.value.code == "encode_failed"


def test_live_payload_with_the_wrong_revision_is_rejected(monkeypatch) -> None:
    class Completed:
        returncode = 0
        stdout = json.dumps({"embedding_revision": "f" * 40, "dimension": DIMENSION, "vectors": [[0.1] * DIMENSION]})
        stderr = ""

    monkeypatch.setattr("aether_worker.retrieval.subprocess.run", lambda *_args, **_kwargs: Completed())
    with pytest.raises(RetrievalError) as rejected:
        encode_texts(["another unmeasured sentence"])
    assert rejected.value.code == "revision_mismatch"
    assert EMBEDDING_REVISION != "f" * 40
