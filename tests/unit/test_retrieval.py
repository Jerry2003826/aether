import hashlib
import json
from contextlib import nullcontext

import bm25s
import pytest

from aether_worker.pending_jobs import _publish_index
from aether_worker.retrieval import (
    DIMENSION,
    EMBEDDING_REVISION,
    RetrievalError,
    _bounded_lexical,
    _index_bm25,
    _lexical_ids,
    _measured,
    _sha,
    encode_texts,
    group_metrics,
    require_tokenizer_version,
    rrf,
    search,
    vectors_for_index,
)
from aether_worker.tokenize import TOKENIZER_VERSION, tokenize


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


def test_baked_pdf_phrase_is_not_measured() -> None:
    assert _sha("苹果派烘烤说明") not in _measured()["vectors"]


def test_unmeasured_text_does_not_start_a_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aether_worker.retrieval.embedding_runtime_ready", lambda: False)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("live encode started")

    monkeypatch.setattr("aether_worker.retrieval.subprocess.run", boom)
    assert vectors_for_index(["苹果派烘烤说明"], allow_download=False) == [None]


def test_measured_text_is_kept_when_download_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    apple = "苹果派的烘烤温度是一百八十度"
    phrase = "苹果派烘烤说明"
    monkeypatch.setattr("aether_worker.retrieval.embedding_runtime_ready", lambda: False)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("live encode started")

    monkeypatch.setattr("aether_worker.retrieval.subprocess.run", boom)
    apple_vector, skipped = vectors_for_index([apple, phrase], allow_download=False)
    assert skipped is None
    assert apple_vector is not None
    assert len(apple_vector) == DIMENSION
    assert _sha(apple) in _measured()["vectors"]


def test_ingestion_publish_refuses_embedding_download(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seen: dict[str, object] = {}

    def fake_build(*_args: object, **kwargs: object) -> str:
        seen.update(kwargs)
        return "gen-1"

    monkeypatch.setattr("aether_worker.pending_jobs.build_generation", fake_build)
    monkeypatch.setattr("aether_worker.pending_jobs._activate", lambda *_args, **_kwargs: None)
    _publish_index(object(), object(), owner_id="owner", project_id="project", index_root=tmp_path, job_id="abc")
    assert seen["allow_download"] is False
    assert seen["force"] is True


def test_explicit_index_build_still_encodes_unmeasured_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aether_worker.retrieval.embedding_runtime_ready", lambda: False)
    seen: list[list[str]] = []

    def fake(texts: list[str]) -> list[list[float]]:
        seen.append(list(texts))
        return [[0.1] * DIMENSION for _item in texts]

    monkeypatch.setattr("aether_worker.retrieval._encode_live", fake)
    vectors = vectors_for_index(["苹果派烘烤说明"], allow_download=True)
    assert seen == [["苹果派烘烤说明"]]
    assert vectors[0] is not None
    assert len(vectors[0]) == DIMENSION


def test_cache_miss_does_not_invent_a_vector(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("model process is not available")

    monkeypatch.setattr("aether_worker.retrieval.subprocess.run", boom)
    with pytest.raises(RetrievalError) as rejected:
        encode_texts(["this sentence is not in the measured table"])
    assert rejected.value.code == "encode_failed"


_OLD_TOKENIZER = "jieba-0.42.1-dict-forward-max+code-symbols"


class _Database:
    def transaction(self, **_kwargs: object) -> object:
        return nullcontext(object())


def _generation(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "gen-1",
        "generation_key": "g1",
        "state": "ACTIVE",
        "manifest_sha256": "unused",
        "tokenizer_version": TOKENIZER_VERSION,
    }
    row.update(overrides)
    return row


def _patch_generation(monkeypatch: pytest.MonkeyPatch, row: dict[str, object]) -> None:
    monkeypatch.setattr("aether_worker.retrieval.current_epoch", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr("aether_worker.retrieval._load_generation", lambda *_args, **_kwargs: row)


def test_require_tokenizer_version_rejects_the_previous_cut() -> None:
    assert TOKENIZER_VERSION != _OLD_TOKENIZER
    require_tokenizer_version(TOKENIZER_VERSION)
    with pytest.raises(RetrievalError) as rejected:
        require_tokenizer_version(_OLD_TOKENIZER)
    assert rejected.value.code == "tokenizer_mismatch"


def test_active_generation_with_old_tokenizer_is_refused_before_cache(tmp_path, monkeypatch) -> None:
    _patch_generation(monkeypatch, _generation(tokenizer_version=_OLD_TOKENIZER))

    def cached(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise AssertionError("cache")

    monkeypatch.setattr("aether_worker.retrieval.read_cached_result", cached)
    with pytest.raises(RetrievalError) as rejected:
        search(_Database(), owner_id="owner", project_id="project", query="苹果派", index_root=tmp_path, mode="lexical")
    assert rejected.value.code == "tokenizer_mismatch"


def test_active_manifest_with_old_tokenizer_is_refused_before_bm25(tmp_path, monkeypatch) -> None:
    manifest = {
        "b": 0.75,
        "chunk_ids": ["chunk-1"],
        "embedding_revision": EMBEDDING_REVISION,
        "k1": 1.2,
        "method": "lucene",
        "tokenizer_version": _OLD_TOKENIZER,
    }
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    directory = tmp_path / "g1"
    directory.mkdir()
    (directory / "manifest.json").write_bytes(raw)
    _patch_generation(monkeypatch, _generation(manifest_sha256=hashlib.sha256(raw).hexdigest()))
    monkeypatch.setattr("aether_worker.retrieval.read_cached_result", lambda *_args, **_kwargs: None)

    def lexical(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("bm25")

    monkeypatch.setattr("aether_worker.retrieval._bounded_lexical", lexical)
    with pytest.raises(RetrievalError) as rejected:
        search(_Database(), owner_id="owner", project_id="project", query="苹果派", index_root=tmp_path, mode="lexical")
    assert rejected.value.code == "tokenizer_mismatch"


def test_inactive_generation_does_not_search_with_the_old_tokenizer(tmp_path, monkeypatch) -> None:
    _patch_generation(monkeypatch, _generation(state="READY", tokenizer_version=_OLD_TOKENIZER))

    def cached(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise AssertionError("cache")

    monkeypatch.setattr("aether_worker.retrieval.read_cached_result", cached)
    result = search(_Database(), owner_id="owner", project_id="project", query="苹果派", index_root=tmp_path, mode="lexical")
    assert result["stale"] is True
    assert result["state"] == "READY"


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
