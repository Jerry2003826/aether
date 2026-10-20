"""group_metrics scores against every relevant document, not only the retrieved ones (review of 84c9e77).

The hand-written nDCG built its ideal ranking by re-sorting the retrieved flags, so a relevant document
that was never retrieved was missing from the IDCG denominator: relevant {A, B} and results [A, C]
scored a perfect 1.0. A duplicate id was also counted once per position, inflating DCG and taking a
cutoff slot. The metrics now come from ir_measures (trec_eval semantics) over qrels built from all
relevant documents: nDCG@k with linear gains and IDCG over every relevant document, R@k over every
relevant document, RR@k; a repeated id counts once, at its first rank, and the cutoff counts distinct
ids. `relevant` may be graded ({id: grade}); a grade of 0 or less is not relevant.

Every expected value below is worked out by hand from the formulas; ir_measures is not called here.
"""

from __future__ import annotations

import math

import pytest
from aether_worker.retrieval import group_metrics


def _gain(rank: int) -> float:
    """1/log2(rank + 1): the discount of a gain-1 hit at a 1-based rank."""
    return 1.0 / math.log2(rank + 1)


def _approx(value: float) -> object:
    return pytest.approx(value, abs=1e-9)


def test_a_relevant_document_that_was_not_retrieved_counts_in_the_ideal_ranking() -> None:
    found = group_metrics({"bm25": ["A", "C"]}, {"A", "B"}, k=2)["bm25"]
    expected = 1.0 / (1.0 + 1.0 / math.log2(3))
    assert expected == _approx(0.6131471927654584)
    assert found["ndcg_at_10"] == _approx(expected)
    assert found["recall_at_10"] == _approx(0.5)
    assert found["mrr_at_10"] == _approx(1.0)


def test_the_default_cutoff_still_counts_the_missed_document() -> None:
    found = group_metrics({"dense": ["A", "C"]}, {"A", "B"})["dense"]
    assert found["ndcg_at_10"] == _approx(1.0 / (1.0 + _gain(2)))


def test_a_duplicate_id_counts_once_at_its_first_rank() -> None:
    found = group_metrics({"rrf": ["A", "A"]}, {"A", "B"}, k=2)["rrf"]
    # Distinct ranking [A]: DCG = 1, IDCG over {A, B} = 1 + 1/log2(3).
    assert found["ndcg_at_10"] == _approx(1.0 / (1.0 + _gain(2)))
    assert found["recall_at_10"] == _approx(0.5)
    assert found["mrr_at_10"] == _approx(1.0)


def test_a_duplicate_does_not_take_a_cutoff_slot() -> None:
    found = group_metrics({"rrf": ["A", "A", "B"]}, {"A", "B"}, k=2)["rrf"]
    # Distinct ranking [A, B]: both relevant documents are inside the cutoff.
    assert found["ndcg_at_10"] == _approx(1.0)
    assert found["recall_at_10"] == _approx(1.0)


@pytest.mark.parametrize(
    ("k", "ndcg", "recall", "mrr"),
    [
        (1, 0.0, 0.0, 0.0),
        (3, _gain(2) / (1.0 + _gain(2) + _gain(3)), 1.0 / 3.0, 0.5),
        (5, (_gain(2) + _gain(4) + _gain(5)) / (1.0 + _gain(2) + _gain(3)), 1.0, 0.5),
    ],
    ids=["cutoff-1", "cutoff-3", "cutoff-5"],
)
def test_each_cutoff_scores_only_its_prefix_against_every_relevant_document(k: int, ndcg: float, recall: float, mrr: float) -> None:
    found = group_metrics({"bm25": ["X", "A", "Y", "B", "C"]}, {"A", "B", "C"}, k=k)["bm25"]
    assert found["ndcg_at_10"] == _approx(ndcg)
    assert found["recall_at_10"] == _approx(recall)
    assert found["mrr_at_10"] == _approx(mrr)


def test_graded_relevance_uses_the_grades_as_gains() -> None:
    found = group_metrics({"rerank": ["B", "A", "C"]}, {"A": 2, "B": 1, "C": 0}, k=3)["rerank"]
    # DCG = 1/log2(2) + 2/log2(3); IDCG = 2/log2(2) + 1/log2(3); C (grade 0) is not relevant.
    expected = (1.0 + 2.0 * _gain(2)) / (2.0 + _gain(2))
    assert expected == _approx(0.8597186998521972)
    assert found["ndcg_at_10"] == _approx(expected)
    assert found["recall_at_10"] == _approx(1.0)
    assert found["mrr_at_10"] == _approx(1.0)


def test_a_grade_zero_document_alone_is_not_a_hit() -> None:
    found = group_metrics({"dense": ["C", "A"]}, {"A": 1, "C": 0}, k=2)["dense"]
    assert found["ndcg_at_10"] == _approx(_gain(2) / 1.0)
    assert found["recall_at_10"] == _approx(1.0)
    assert found["mrr_at_10"] == _approx(0.5)


def test_nothing_relevant_or_nothing_retrieved_is_zero_and_the_shape_is_unchanged() -> None:
    found = group_metrics({"bm25": ["A"], "dense": []}, set())
    assert found == {
        "bm25": {"recall_at_10": 0.0, "mrr_at_10": 0.0, "ndcg_at_10": 0.0},
        "dense": {"recall_at_10": 0.0, "mrr_at_10": 0.0, "ndcg_at_10": 0.0},
    }
    empty = group_metrics({"dense": []}, {"A"})["dense"]
    assert empty == {"recall_at_10": 0.0, "mrr_at_10": 0.0, "ndcg_at_10": 0.0}
