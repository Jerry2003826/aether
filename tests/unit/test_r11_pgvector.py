"""Vectors sent to pgvector keep every float32 value and never carry NaN or infinity."""

from __future__ import annotations

import math

import numpy as np
import pytest
from aether_worker.retrieval import RetrievalError, _vector_literal


def _parse(literal: str) -> list[float]:
    assert literal.startswith("[") and literal.endswith("]")
    return [float(part) for part in literal[1:-1].split(",")]


def test_vector_param_is_float32_exact() -> None:
    values = [1e-9, -3.5e-12, 0.123456789123, 12345.678901, -0.000000049]
    expected = [float(value) for value in np.asarray(values, dtype=np.float32)]
    parsed = _parse(_vector_literal(values))
    assert [float(value) for value in np.asarray(parsed, dtype=np.float32)] == expected
    assert parsed[0] != 0.0


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_vector_rejected(bad: float) -> None:
    with pytest.raises(RetrievalError) as caught:
        _vector_literal([0.5, bad, 0.25])
    assert caught.value.code == "vector_not_finite"
