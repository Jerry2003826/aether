"""The retention search cache is bounded and still honours epoch, generation and project drop."""

from __future__ import annotations

import threading
from typing import Any

from aether_worker import retention

LIMIT = 1024


class _Epoch:
    def __init__(self, epoch: int) -> None:
        self.epoch = epoch

    def execute(self, *_args: Any, **_kwargs: Any) -> _Epoch:
        return self

    def scalar_one(self) -> int:
        return self.epoch


def _clear() -> None:
    for project in {key[0] for key in list(retention._SEARCH_CACHE)}:
        retention._drop_project_cache(project)


def test_search_cache_is_bounded() -> None:
    _clear()
    for index in range(LIMIT * 2):
        retention.cache_result("p-bound", 1, f"q{index}", {"n": index})
    assert len(retention._SEARCH_CACHE) <= LIMIT
    assert retention.read_cached_result(_Epoch(1), "p-bound", f"q{LIMIT * 2 - 1}") == {"n": LIMIT * 2 - 1}
    _clear()


def test_bounded_cache_keeps_epoch_generation_and_project_drop() -> None:
    _clear()
    retention.cache_result("p-a", 3, "q", {"v": 1}, generation_id="g1")
    retention.cache_result("p-b", 3, "q", {"v": 2}, generation_id="g1")
    assert retention.read_cached_result(_Epoch(3), "p-a", "q", generation_id="g1") == {"v": 1}
    assert retention.read_cached_result(_Epoch(4), "p-a", "q", generation_id="g1") is None
    assert retention.read_cached_result(_Epoch(3), "p-a", "q", generation_id="g2") is None
    retention._drop_project_cache("p-a")
    assert retention.read_cached_result(_Epoch(3), "p-a", "q", generation_id="g1") is None
    assert retention.read_cached_result(_Epoch(3), "p-b", "q", generation_id="g1") == {"v": 2}
    _clear()


def test_bounded_cache_survives_concurrent_writers_and_drops() -> None:
    _clear()
    errors: list[BaseException] = []

    def write(worker: int) -> None:
        try:
            for index in range(LIMIT):
                retention.cache_result(f"p{worker % 3}", 1, f"{worker}-{index}", {"i": index})
                if index % 97 == 0:
                    retention._drop_project_cache(f"p{(worker + 1) % 3}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(worker,)) for worker in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(retention._SEARCH_CACHE) <= LIMIT
    _clear()
