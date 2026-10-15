"""Project-scoped hybrid retrieval over one immutable BM25 generation and pgvector."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import bm25s
from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.retention import commit_embedding, current_epoch
from aether_worker.tokenize import TOKENIZER_VERSION, tokenize

ROOT = Path(__file__).resolve().parents[3]
ML_ENV = ROOT / "environments" / "ml"
FIXTURE = ROOT / "config" / "retrieval" / "fixture.json"
MEASUREMENTS = ROOT / "config" / "retrieval" / "measured.json"
LOCK = json.loads((ROOT / "config" / "models.lock.json").read_text(encoding="utf-8"))
EMBEDDING_REVISION = str(LOCK["ml"]["embedding_revision"])
DIMENSION = int(LOCK["ml"]["dimension_probed"])
TOP_K = 50
RRF_K = 60
RERANK_HEAD = 30
CONTEXT_LIMIT = 8
PER_SOURCE = 2
DEBOUNCE_SECONDS = 30
_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_CACHE: tuple[object, dict[str, Any]] | None = None


class RetrievalError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _read_measurement(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("embedding_revision") != EMBEDDING_REVISION or payload.get("dimension") != DIMENSION:
        raise RetrievalError("revision_mismatch")
    return payload


def _measured() -> dict[str, Any]:
    global _CACHE
    fixture_stamp = FIXTURE.stat().st_mtime if FIXTURE.is_file() else 0
    full_stamp = MEASUREMENTS.stat().st_mtime if MEASUREMENTS.is_file() else 0
    key = (str(FIXTURE), fixture_stamp, str(MEASUREMENTS), full_stamp)
    if _CACHE is not None and _CACHE[0] == key:
        return _CACHE[1]
    if FIXTURE.is_file():
        payload = _read_measurement(FIXTURE)
    else:
        payload = {"embedding_revision": EMBEDDING_REVISION, "dimension": DIMENSION, "vectors": {}, "rerank_scores": {}}
    if MEASUREMENTS.is_file():
        extra = _read_measurement(MEASUREMENTS)
        payload["vectors"].update(extra.get("vectors") or {})
        payload.setdefault("rerank_scores", {}).update(extra.get("rerank_scores") or {})
        if extra.get("reranker_revision"):
            payload["reranker_revision"] = extra["reranker_revision"]
    _CACHE = (key, payload)
    return payload


def _encode_live(texts: list[str]) -> list[list[float]]:
    try:
        completed = subprocess.run(
            ["uv", "run", "--frozen", "python", "encode_texts.py"],
            cwd=ML_ENV,
            input=json.dumps({"texts": texts}),
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetrievalError("encode_failed") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise RetrievalError("encode_failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RetrievalError("encode_failed") from exc
    if payload.get("embedding_revision") != EMBEDDING_REVISION or payload.get("dimension") != DIMENSION:
        raise RetrievalError("revision_mismatch")
    vectors = payload.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RetrievalError("encode_failed")
    parsed: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != DIMENSION:
            raise RetrievalError("dimension_mismatch")
        parsed.append([float(value) for value in vector])
    return parsed


def encode_texts(texts: list[str]) -> list[list[float]]:
    """Return the measured BGE-M3 vectors. Unknown texts go to the CPU encode process."""
    table = _measured()
    vectors: dict[str, list[float]] = table["vectors"]
    missing = [text for text in texts if _sha(text) not in vectors]
    live = {}
    if missing:
        for text, vector in zip(missing, _encode_live(missing), strict=True):
            live[_sha(text)] = vector
    chosen: list[list[float]] = []
    for text in texts:
        key = _sha(text)
        vector = vectors[key] if key in vectors else live[key]
        if len(vector) != DIMENSION:
            raise RetrievalError("dimension_mismatch")
        chosen.append(vector)
    return chosen


def _rerank_live(pairs: list[list[str]]) -> list[float]:
    try:
        completed = subprocess.run(
            ["uv", "run", "--frozen", "python", "rerank_pairs.py"],
            cwd=ML_ENV,
            input=json.dumps({"pairs": pairs}),
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetrievalError("rerank_failed") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise RetrievalError("rerank_failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RetrievalError("rerank_failed") from exc
    expected = _measured().get("reranker_revision")
    if expected and payload.get("reranker_revision") != expected:
        raise RetrievalError("revision_mismatch")
    scores = payload.get("scores")
    if not isinstance(scores, list) or len(scores) != len(pairs):
        raise RetrievalError("rerank_failed")
    return [float(score) for score in scores]


def rerank_scores(query: str, passages: list[str]) -> list[float]:
    table = _measured()
    stored: dict[str, float] = table.get("rerank_scores", {})
    scores: list[float | None] = []
    missing: list[int] = []
    for index, passage in enumerate(passages):
        key = _sha(query + "\n" + passage)
        if key in stored:
            scores.append(float(stored[key]))
        else:
            scores.append(None)
            missing.append(index)
    if missing:
        live = _rerank_live([[query, passages[index]] for index in missing])
        for index, score in zip(missing, live, strict=True):
            scores[index] = score
    return [float(score) for score in scores]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


def rrf(rank_lists: list[list[str]], *, k: int = RRF_K) -> list[str]:
    """Fuse ranks. Rank starts at 1. A repeated id in one list counts once."""
    scores: dict[str, float] = {}
    for ranked in rank_lists:
        seen: set[str] = set()
        for rank, chunk_id in enumerate(ranked, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))


def group_metrics(groups: dict[str, list[str]], relevant: set[str], *, k: int = 10) -> dict[str, dict[str, float]]:
    """Recall, MRR, and nDCG for each channel. This does not claim one channel wins."""

    def recall(ids: list[str]) -> float:
        if not relevant:
            return 0.0
        return len(set(ids[:k]) & relevant) / len(relevant)

    def mrr(ids: list[str]) -> float:
        for index, chunk_id in enumerate(ids[:k], start=1):
            if chunk_id in relevant:
                return 1.0 / index
        return 0.0

    def ndcg(ids: list[str]) -> float:
        flags = [1.0 if chunk_id in relevant else 0.0 for chunk_id in ids[:k]]
        ideal = sorted(flags, reverse=True)

        def dcg(values: list[float]) -> float:
            return sum(value / math.log2(index + 2) for index, value in enumerate(values))

        denom = dcg(ideal)
        return 0.0 if denom == 0 else dcg(flags) / denom

    return {name: {"recall_at_10": recall(ids), "mrr_at_10": mrr(ids), "ndcg_at_10": ndcg(ids)} for name, ids in groups.items()}


def generation_is_fresh(conn: Any, project_id: str, seen_epoch: int, chunk_ids: list[str]) -> bool:
    if current_epoch(conn, project_id) != int(seen_epoch):
        return False
    visible = {row["id"] for row in _visible(conn, project_id)}
    return all(chunk_id in visible for chunk_id in chunk_ids)


def _bind(conn: Any, owner_id: str, project_id: str) -> None:
    conn.execute(text("SELECT set_config('aether.owner_id', :owner_id, true)"), {"owner_id": owner_id})
    conn.execute(text("SELECT set_config('aether.project_id', :project_id, true)"), {"project_id": project_id})


def _visible(conn: Any, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT c.id::text AS id, c.text_content, c.source_version_id::text AS source_version_id, "
            "c.start_offset, c.end_offset, c.embedding_revision, v.content_sha256, s.id::text AS source_id "
            "FROM chunks c "
            "JOIN source_versions v ON v.project_id = c.project_id AND v.id = c.source_version_id "
            "JOIN sources s ON s.project_id = v.project_id AND s.id = v.source_id "
            "WHERE c.project_id = :project_id AND c.deleted_at IS NULL AND s.deleted_at IS NULL AND s.status <> 'revoked' "
            "ORDER BY c.id"
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _index_bm25(texts: list[str], destination: Path) -> None:
    tokens = [tokenize(text) for text in texts]
    retriever = bm25s.BM25(method="lucene", k1=1.2, b=0.75)
    retriever.index(tokens, show_progress=False, leave_progress=False)
    destination.mkdir(parents=True, exist_ok=False)
    retriever.save(str(destination), corpus=None, show_progress=False, leave_progress=False)


def _lexical_ids(index_dir: Path, chunk_ids: list[str], query: str) -> list[str]:
    if not chunk_ids:
        return []
    query_tokens = [token for token in tokenize(query)]
    retriever = bm25s.BM25.load(str(index_dir), load_corpus=False, mmap=False)
    known = [token for token in query_tokens if token in retriever.vocab_dict]
    if not known:
        return []
    window = min(len(chunk_ids), 200)
    found, _scores = retriever.retrieve([known], k=window, show_progress=False, leave_progress=False)
    return [chunk_ids[int(position)] for position in found[0]]


def _check_key(generation_key: str, root: Path, final: Path) -> None:
    if _KEY.fullmatch(generation_key) is None:
        raise RetrievalError("generation_key_rejected")
    if not final.resolve().is_relative_to(root.resolve()):
        raise RetrievalError("generation_key_rejected")


def build_generation(
    database: Database,
    store: ArtifactStore,
    *,
    owner_id: str,
    project_id: str,
    generation_key: str,
    index_root: Path,
    force: bool = False,
    now: datetime | None = None,
) -> str:
    """Snapshot visible chunks, write a temp BM25 directory, then publish one READY generation."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    root = index_root.resolve()
    final = root / generation_key
    partial = root / f".{generation_key}.partial"
    _check_key(generation_key, root, final)
    root.mkdir(parents=True, exist_ok=True)
    with database.engine.connect() as conn:
        _bind(conn, owner_id, project_id)
        locked = conn.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:project_id, 11))"),
            {"project_id": project_id},
        ).scalar_one()
        conn.commit()
        if not locked:
            raise RetrievalError("build_busy")
        try:
            _bind(conn, owner_id, project_id)
            if not force:
                recent = conn.execute(
                    text(
                        "SELECT id::text AS id, created_at FROM index_generations "
                        "WHERE project_id = :project_id AND state IN ('READY', 'ACTIVE') "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"project_id": project_id},
                ).mappings().first()
                if recent is not None and (moment - recent["created_at"]).total_seconds() < DEBOUNCE_SECONDS:
                    conn.commit()
                    return str(recent["id"])
            rows = _visible(conn, project_id)
            if not rows:
                raise RetrievalError("empty_index")
            seen_epoch = current_epoch(conn, project_id)
            missing = [row["text_content"] for row in rows if row["embedding_revision"] != EMBEDDING_REVISION]
            vectors = encode_texts(missing) if missing else []
            encoded = dict(zip(missing, vectors, strict=True))
            for row in rows:
                if row["text_content"] not in encoded:
                    continue
                wrote = commit_embedding(
                    conn,
                    project_id=project_id,
                    chunk_id=row["id"],
                    embedding=_vector_literal(encoded[row["text_content"]]),
                    revision=EMBEDDING_REVISION,
                    seen_epoch=seen_epoch,
                )
                if not wrote:
                    conn.rollback()
                    raise RetrievalError("stale_visibility")
            if not generation_is_fresh(conn, project_id, seen_epoch, [row["id"] for row in rows]):
                conn.rollback()
                raise RetrievalError("stale_visibility")
            conn.commit()
            _bind(conn, owner_id, project_id)
            if partial.exists():
                shutil.rmtree(partial)
            _index_bm25([row["text_content"] for row in rows], partial)
            chunk_ids = [row["id"] for row in rows]
            manifest = {
                "chunk_ids": chunk_ids,
                "embedding_revision": EMBEDDING_REVISION,
                "tokenizer_version": TOKENIZER_VERSION,
                "method": "lucene",
                "k1": 1.2,
                "b": 0.75,
            }
            raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
            (partial / "manifest.json").write_bytes(raw)
            loaded = _lexical_ids(partial, chunk_ids, rows[0]["text_content"])
            if not (partial / "vocab.index.json").is_file() or len(loaded) < 1:
                raise RetrievalError("index_rejected")
            if final.exists():
                raise RetrievalError("generation_key_rejected")
            _bind(conn, owner_id, project_id)
            if not generation_is_fresh(conn, project_id, seen_epoch, chunk_ids):
                raise RetrievalError("stale_visibility")
            os.replace(partial, final)
            stored = store.put(owner_id=owner_id, project_id=project_id, data=raw, media_type="application/json")
            generation_id = conn.execute(
                text(
                    "INSERT INTO index_generations (project_id, generation_key, tokenizer_version, source_watermark, "
                    "manifest_artifact_id, state, manifest_sha256) "
                    "VALUES (:project_id, :generation_key, :tokenizer, :watermark, :artifact_id, 'READY', :sha) "
                    "RETURNING id::text"
                ),
                {
                    "project_id": project_id,
                    "generation_key": generation_key,
                    "tokenizer": TOKENIZER_VERSION,
                    "watermark": len(chunk_ids),
                    "artifact_id": stored["artifact_id"],
                    "sha": stored["sha256"],
                },
            ).scalar_one()
            conn.commit()
            return str(generation_id)
        finally:
            if partial.exists():
                shutil.rmtree(partial, ignore_errors=True)
            conn.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:project_id, 11))"),
                {"project_id": project_id},
            )
            conn.commit()


def activate_generation(database: Database, *, owner_id: str, project_id: str, generation_id: str) -> None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        conn.execute(
            text("UPDATE index_generations SET state = 'RETIRED' WHERE project_id = :project_id AND state = 'ACTIVE'"),
            {"project_id": project_id},
        )
        updated = conn.execute(
            text(
                "UPDATE index_generations SET state = 'ACTIVE', activated_at = now() "
                "WHERE project_id = :project_id AND id = :id AND state = 'READY'"
            ),
            {"project_id": project_id, "id": generation_id},
        ).rowcount
    if updated != 1:
        raise RetrievalError("activation_rejected")


def _load_generation(conn: Any, project_id: str, generation_id: str | None) -> dict[str, Any] | None:
    if generation_id is None:
        row = conn.execute(
            text(
                "SELECT id::text AS id, generation_key, state, manifest_sha256 FROM index_generations "
                "WHERE project_id = :project_id AND state = 'ACTIVE'"
            ),
            {"project_id": project_id},
        ).mappings().first()
    else:
        row = conn.execute(
            text(
                "SELECT id::text AS id, generation_key, state, manifest_sha256 FROM index_generations "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": generation_id},
        ).mappings().first()
    return None if row is None else dict(row)


def _empty(stale: bool, *, state: str | None = None) -> dict[str, Any]:
    return {
        "stale": stale,
        "state": state,
        "degraded": False,
        "query": "",
        "rewrites": [],
        "bm25": [],
        "dense": [],
        "rrf": [],
        "rerank": [],
        "context": "",
        "citations": [],
        "index_lag": False,
        "embedding_revision": EMBEDDING_REVISION,
    }


def _select_context(ordered: list[str], rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    used = 0
    for chunk_id in ordered:
        row = rows.get(chunk_id)
        if row is None:
            continue
        if counts.get(row["source_id"], 0) >= PER_SOURCE:
            continue
        if chosen and used + len(row["text_content"]) > 2000:
            break
        chosen.append(row)
        counts[row["source_id"]] = counts.get(row["source_id"], 0) + 1
        used += len(row["text_content"])
        if len(chosen) >= CONTEXT_LIMIT:
            break
    return chosen


@contextmanager
def _reader(index_dir: Path) -> Iterator[None]:
    index_dir.mkdir(parents=True, exist_ok=True)
    handle = (index_dir / ".search.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def search(
    database: Database,
    *,
    owner_id: str,
    project_id: str,
    query: str,
    index_root: Path,
    generation_id: str | None = None,
) -> dict[str, Any]:
    query_vector = encode_texts([query])[0]
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        generation = _load_generation(conn, project_id, generation_id)
        if generation is None:
            raise RetrievalError("generation_missing")
        if generation["state"] != "ACTIVE":
            result = _empty(True, state=generation["state"])
            result["query"] = query
            return result
        index_dir = (index_root / generation["generation_key"]).resolve()
        manifest_path = index_dir / "manifest.json"
        if not manifest_path.is_file():
            raise RetrievalError("manifest_rejected")
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != generation["manifest_sha256"]:
            raise RetrievalError("manifest_rejected")
        manifest = json.loads(raw)
        if manifest.get("embedding_revision") != EMBEDDING_REVISION:
            raise RetrievalError("revision_mismatch")
        visible = _visible(conn, project_id)
        allowed = {row["id"]: row for row in visible}
        chunk_ids = [chunk_id for chunk_id in manifest["chunk_ids"] if chunk_id in allowed]
        index_lag = any(row["id"] not in set(manifest["chunk_ids"]) for row in visible)
        if chunk_ids:
            dense_rows = conn.execute(
                text(
                    "SELECT c.id::text FROM chunks c "
                    "JOIN source_versions v ON v.project_id = c.project_id AND v.id = c.source_version_id "
                    "JOIN sources s ON s.project_id = v.project_id AND s.id = v.source_id "
                    "WHERE c.project_id = :project_id AND c.id::text = ANY(:ids) "
                    "AND c.deleted_at IS NULL AND s.deleted_at IS NULL AND s.status <> 'revoked' "
                    "AND c.embedding_revision = :revision "
                    "ORDER BY c.embedding <=> CAST(:query AS vector) LIMIT :limit"
                ),
                {
                    "project_id": project_id,
                    "ids": chunk_ids,
                    "revision": EMBEDDING_REVISION,
                    "query": _vector_literal(query_vector),
                    "limit": TOP_K,
                },
            ).scalars().all()
        else:
            dense_rows = []
    dense = [str(chunk_id) for chunk_id in dense_rows]
    with _reader(index_dir):
        bm25 = [chunk_id for chunk_id in _lexical_ids(index_dir, list(manifest["chunk_ids"]), query) if chunk_id in allowed][:TOP_K]
        fused = rrf([bm25, dense])
        head = fused[:RERANK_HEAD]
        degraded = False
        try:
            scores = rerank_scores(query, [allowed[chunk_id]["text_content"] for chunk_id in head])
            order = sorted(range(len(head)), key=lambda index: (-scores[index], head[index]))
            reranked = [head[index] for index in order]
        except RetrievalError:
            reranked = list(fused)
            degraded = True
        chosen = _select_context(reranked, allowed)
        return {
            "stale": False,
            "state": "ACTIVE",
            "degraded": degraded,
            "query": query,
            "rewrites": [],
            "bm25": bm25,
            "dense": dense,
            "rrf": fused,
            "rerank": reranked,
            "context": "\n".join(row["text_content"] for row in chosen),
            "citations": [
                {
                    "chunk_id": row["id"],
                    "source_version_id": row["source_version_id"],
                    "content_sha256": row["content_sha256"],
                    "start_offset": row["start_offset"],
                    "end_offset": row["end_offset"],
                }
                for row in chosen
            ],
            "index_lag": index_lag,
            "embedding_revision": EMBEDDING_REVISION,
        }


def release_retired(database: Database, *, owner_id: str, project_id: str, index_root: Path) -> list[str]:
    """Delete retired generation directories once no search holds the shared lock."""
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        rows = conn.execute(
            text("SELECT generation_key FROM index_generations WHERE project_id = :project_id AND state = 'RETIRED'"),
            {"project_id": project_id},
        ).scalars().all()
    removed: list[str] = []
    for key in rows:
        directory = index_root / str(key)
        if not directory.is_dir():
            continue
        lock_path = directory / ".search.lock"
        handle = lock_path.open("a+") if lock_path.exists() else None
        try:
            if handle is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            shutil.rmtree(directory)
            removed.append(str(key))
        except BlockingIOError:
            continue
        finally:
            if handle is not None:
                handle.close()
    return removed
